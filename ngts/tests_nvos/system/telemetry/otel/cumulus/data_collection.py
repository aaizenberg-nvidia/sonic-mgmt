"""Cumulus telemetry collection (stop/copy/restart) and cache population for mgmt VRF tests."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.telemetry.otel.cumulus import cache as telemetryCache
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_telemetry import (
    collect_cli_mgmt_vrf_session_data,
    collect_cli_platform_only,
    get_cp_stat_sx_api_cli,
    collect_control_plane_stats_only
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.comparison_logging import (
    log_cli_cache_summary,
    log_parsed_otel_payload_summary,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import (
    apply_otel_mgmt_vrf_no_tls_telemetry_config,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.validations import OtelDataValidations
from ngts.tests_nvos.system.telemetry.otel.constants import OtelCollectorConst
from ngts.tests_nvos.system.telemetry.otel.helpers import _is_prometheus_sidecar_metric
from ngts.tests_nvos.system.telemetry.otel.otel_collector import OtelCollector

logger = logging.getLogger(__name__)

OTEL_CACHE_PAYLOAD_KEYS = (
    "metrics_timestamps",
    "platform_stats",
    "cp_stats",
    "intf_stats",
    "histograms",
    "hist_list",
)

OTEL_ARTIFACT_FILENAME = "test-otel-out.json"
DATA_COLLECTION_SUBDIR = "data_collection"
_SERVICE_SETTLE_SEC = 5


def settle_dut_ssh_after_module_transition(
    dut,
    *,
    settle_sec: int = CumulusOtelConst.COEXISTENCE_MODULE_TRANSITION_SETTLE_SEC,
) -> None:
    """Recover SSH/SCP after heavy NVUE teardown or coexistence module handoff."""
    with allure.step('Settle DUT SSH channel (%ds)' % settle_sec):
        engine = getattr(dut, 'engine', None)
        if engine is not None:
            clear = getattr(engine, 'clear_buffer', None)
            if callable(clear):
                try:
                    clear()
                except Exception:
                    pass
        dut.run_cmd('true', validate=False, print_output=False)
        time.sleep(settle_sec)


def cleanup_stale_nvue_censor_files(dut) -> None:
    """Remove stale NVUE CLI censorship tempfiles (``/tmp/cen*``) left on mlx DUT shells.

    When these files are owned by another session, bash appends ``Permission denied`` noise
    to every command and NVUE ``nv action import`` fails writing a new censorship file.
    """
    with allure.step("Clean stale NVUE censorship tempfiles on DUT (/tmp/cen*)"):
        dut.run_cmd(
            "sudo rm -f /tmp/cen* 2>/dev/null; true",
            validate=False,
            print_output=False,
        )


def _parse_systemctl_is_active(raw: str) -> str:
    """Return the ``systemctl is-active`` token, ignoring mlx bash cen* prompt noise."""
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if (
            line.startswith("-bash:") or
            line.startswith("rm:") or
            line.startswith("sudo ") or
            "Permission denied" in line or
            "cumulus@" in line
        ):
            continue
        return line
    return (raw or "").strip()


def _asic_monitor_active(dut, vrf: str) -> bool:
    """True when ``asic-monitor@<vrf>`` is running or histogram export is healthy."""
    unit = f"asic-monitor@{vrf}"
    if _parse_systemctl_is_active(
        dut.run_cmd(
            f"systemctl is-active {unit} 2>&1",
            validate=False,
            print_output=False,
        )
    ) == "active":
        return True
    from ngts.tests_nvos.system.telemetry.otel.cumulus.telemetry_health import (
        show_telemetry_health,
    )

    health = show_telemetry_health(dut)
    hist_state = (health.get("service-status") or {}).get("histogram-export-service")
    return hist_state == "active"


def data_collection_dir(output_dir: str) -> str:
    path = os.path.join(output_dir, DATA_COLLECTION_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def cumulus_otel_artifact_path(output_dir: str) -> str:
    return os.path.join(data_collection_dir(output_dir), OTEL_ARTIFACT_FILENAME)


def _nvtelemetry_unit(vrf: str) -> str:
    return f"nv-telemetry@{vrf}"


def stop_nvtelemetry(dut, vrf: str, unit: Optional[str] = None) -> None:
    unit = unit or _nvtelemetry_unit(vrf)
    with allure.step(f"Stop {unit}"):
        dut.run_cmd(f"sudo systemctl stop {unit}", validate=False)
        time.sleep(_SERVICE_SETTLE_SEC)


def start_nvtelemetry(dut, vrf: str, unit: Optional[str] = None) -> None:
    unit = unit or _nvtelemetry_unit(vrf)
    with allure.step(f"Start {unit}"):
        dut.run_cmd(f"sudo systemctl start {unit}", validate=False)
        time.sleep(_SERVICE_SETTLE_SEC)


def restart_nvtelemetry(dut, vrf: str, unit: Optional[str] = None) -> None:
    """Restart the telemetry unit (SSIM ``otelClientOps.restart_nvtelemetry``).

    Defaults to the Cumulus per-VRF instance ``nv-telemetry@<vrf>``; pass ``unit``
    (e.g. ``nv-telemetry.service`` on NVOS) to override.
    """
    unit = unit or _nvtelemetry_unit(vrf)
    with allure.step(f"Restart {unit}"):
        dut.run_cmd(f"sudo systemctl restart {unit}", validate=False)
        time.sleep(_SERVICE_SETTLE_SEC)


def ensure_asic_monitor_running(dut, vrf: str) -> None:
    """Enable and start ``asic-monitor@<vrf>`` after NVUE apply.

    On Cumulus 5.17+ the unit may exit successfully (``inactive (dead)``) while
    ``histogram-export-service`` reports active in telemetry health — poll health
    instead of requiring ``systemctl is-active == active``.
    """
    unit = f"asic-monitor@{vrf}"
    with allure.step(f"Ensure {unit} is enabled and histogram export is healthy"):
        dut.run_cmd(f"sudo systemctl enable {unit}", validate=False)
        dut.run_cmd(f"sudo systemctl restart {unit}", validate=False)
        deadline = time.time() + 30
        while time.time() < deadline:
            if _asic_monitor_active(dut, vrf):
                logger.info("%s / histogram-export-service healthy", unit)
                return
            time.sleep(2)
        status = dut.run_cmd(
            f"systemctl status {unit} 2>&1",
            validate=False,
            print_output=False,
        )
        pytest.fail(
            f"{unit} / histogram-export-service not healthy after restart:\n{status}"
        )


def restart_asic_monitor(dut, vrf: str) -> None:
    """Restart ``asic-monitor@<vrf>`` (maps to ``histogram-export-service`` in health)."""
    unit = f"asic-monitor@{vrf}"
    with allure.step(f"Restart {unit}"):
        dut.run_cmd(f"sudo systemctl enable {unit}", validate=False)
        dut.run_cmd(f"sudo systemctl restart {unit}", validate=False)
        time.sleep(_SERVICE_SETTLE_SEC)


def truncate_collector_export(collector: OtelCollector) -> None:
    with allure.step("Truncate collector export (pre-collect cleanup)"):
        collector.truncate_artifact()


def _download_collector_export(server_node: OtelCollector, cur_dir: str) -> str:
    """SCP collector file export to ``<cur_dir>/data_collection/test-otel-out.json``.

    NGTS ``OtelCollector`` writes ``/etc/otelcol/primary-test.json`` (and rotated
    ``primary-test-*.json``), not the legacy ``/etc/otelcol/test.json`` path. After
    ``stop()``, :meth:`OtelCollector.fetch_artifact` locates the newest non-empty
    artifact and SCPs it using the same staging logic as other OTEL tests.
    """
    collect_dir = data_collection_dir(cur_dir)
    with allure.step(f"Fetch OTEL export to {OTEL_ARTIFACT_FILENAME}"):
        return server_node.fetch_artifact(
            collect_dir,
            file_name=OTEL_ARTIFACT_FILENAME,
            timeout_sec=CumulusOtelConst.ARTIFACT_TIMEOUT_SEC,
        )


def collect_telemetry_data(
    client_node,
    server_node: Union[OtelCollector, Any],
    vrf: str,
    sleep_time: int,
    root_dir: Optional[str],
    cur_dir: str,
    *,
    telemetry_unit: Optional[str] = None,
) -> str:
    """Collect OTLP export via stop/copy/restart (Cumulus mgmt VRF workflow).

    ``client_node`` is ``engines.dut``. ``server_node`` is
    :class:`~ngts.tests_nvos.system.telemetry.otel.otel_collector.OtelCollector`
    on sonic-mgmt. ``root_dir`` is unused (kept for call-site compatibility).
    Returns local path to ``<cur_dir>/data_collection/test-otel-out.json``.
    """
    del root_dir

    with allure.step(f"Collect telemetry data ({sleep_time}s, stop/copy/restart)"):
        logger.info(
            "%s waiting %ss before collecting otel output %s",
            "*" * 10,
            sleep_time,
            "*" * 10,
        )
        time.sleep(sleep_time)

        stop_nvtelemetry(client_node, vrf, unit=telemetry_unit)

        if not isinstance(server_node, OtelCollector):
            pytest.fail(
                f"server_node must be OtelCollector, got {type(server_node).__name__}"
            )

        with allure.step("Stop otelcol on collector host"):
            server_node.stop()

        artifact_path = _download_collector_export(server_node, cur_dir)

        size = os.path.getsize(artifact_path) if os.path.exists(artifact_path) else 0
        if size <= 0:
            logger.error("No telemetry data being captured.")
            pytest.fail("No telemetry data captured")
        logger.info("Captured Otel exported file size: %s", size)

        with allure.step("Restart otelcol and nv-telemetry (post-collect)"):
            server_node.ensure_running(install_if_missing=False)
            start_nvtelemetry(client_node, vrf, unit=telemetry_unit)

    return artifact_path


def collect_and_cache_secured_otel_session(
    dut,
    collector: OtelCollector,
    cur_dir: str,
    *,
    vrf: str,
    wait_sec: int,
    cleanup_session: bool = True,
    telemetry_unit: Optional[str] = None,
    is_nvos: bool = False,
) -> Dict[str, Any]:
    """Collect secured OTLP export and populate telemetry cache (SSIM secured test01).

    ``telemetry_unit`` overrides the systemd unit used by the stop/copy/restart
    collection (NVOS passes ``nv-telemetry.service``); defaults to the Cumulus
    per-VRF instance. ``is_nvos`` skips Cumulus-only asic-monitor in cleanup.
    """
    otel_data_val = OtelDataValidations()
    hostname = get_dut_hostname(dut)

    if cleanup_session:
        from ngts.tests_nvos.system.telemetry.otel.cumulus.telemetry_health import (
            cleanup_otlp_export_session,
        )

        cleanup_otlp_export_session(
            dut, collector, vrf=vrf, telemetry_unit=telemetry_unit, is_nvos=is_nvos
        )
        from ngts.tests_nvos.system.telemetry.otel.cumulus.telemetry_health import (
            assert_otlp_session_established,
        )

        assert_otlp_session_established(collector)

    truncate_collector_export(collector)
    collect_telemetry_data(
        dut, collector, vrf, wait_sec, None, cur_dir, telemetry_unit=telemetry_unit
    )

    artifact_path = cumulus_otel_artifact_path(cur_dir)
    return parse_telemetry_and_cache(
        otel_data_val, artifact_path=artifact_path, hostname=hostname
    )


def _classify_metric_names(names: Set[str]) -> Tuple[List[str], List[str], List[str]]:
    """Split metric names into nvswitch, prometheus sidecar, and other."""
    nvswitch = sorted(n for n in names if n.startswith("nvswitch_"))
    prometheus = sorted(n for n in names if _is_prometheus_sidecar_metric(n))
    nvswitch_set = set(nvswitch)
    prometheus_set = set(prometheus)
    other = sorted(n for n in names if n not in nvswitch_set and n not in prometheus_set)
    return nvswitch, prometheus, other


def _metric_names_from_artifact(artifact_path: str) -> Tuple[int, int, Set[str]]:
    """Scan OTLP JSON lines in the collector export; return line stats and all metric names."""
    expanded_path = os.path.expanduser(artifact_path)
    total_lines = 0
    json_errors = 0
    names: Set[str] = set()

    if not os.path.isfile(expanded_path):
        return total_lines, json_errors, names

    with open(expanded_path, "r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line:
                continue
            total_lines += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                json_errors += 1
                continue
            for resource in obj.get("resourceMetrics", []) or []:
                for scope in resource.get("scopeMetrics", []) or []:
                    for metric in scope.get("metrics", []) or []:
                        name = metric.get("name")
                        if name:
                            names.add(name)
    return total_lines, json_errors, names


def _log_sorted_metric_names(
    names: List[str],
    section_title: str,
    *,
    max_names: Optional[int] = None,
) -> None:
    """Emit one INFO line per metric name; optional cap only when ``max_names`` is set."""
    logger.info("  --- %s (%d) ---", section_title, len(names))
    display = names if max_names is None else names[:max_names]
    for name in display:
        logger.info("    %s", name)
    if max_names is not None and len(names) > max_names:
        logger.info("    ... +%d more", len(names) - max_names)


def log_collector_export_metrics(
    artifact_path: str,
    *,
    collector_label: str,
    parsed_metrics_timestamps: Optional[Dict[str, Any]] = None,
    max_metric_names: Optional[int] = None,
) -> None:
    """Log every metric name present in the collector OTLP artifact (debug).

    Reads all non-empty JSON lines from the export file (no hostname or attribute
    filtering). When ``parsed_metrics_timestamps`` is supplied, also logs names that
    differ between the raw artifact and the post-parse cache (e.g. hostname line
    filter in ``parse_disjoint_json``).
    """
    expanded_path = os.path.expanduser(artifact_path)
    file_bytes = os.path.getsize(expanded_path) if os.path.isfile(expanded_path) else 0
    total_lines, json_errors, artifact_names = _metric_names_from_artifact(artifact_path)

    if not os.path.isfile(expanded_path):
        logger.warning("OTEL collector artifact missing: %s", expanded_path)

    nvswitch_names, prometheus_names, other_names = _classify_metric_names(artifact_names)
    artifact_sorted = sorted(artifact_names)
    full_metric_list_text = "\n".join(artifact_sorted)

    parsed_names: Set[str] = set()
    if parsed_metrics_timestamps is not None:
        parsed_names = set(parsed_metrics_timestamps.keys())

    with allure.step(f"Log collector artifact metrics ({collector_label})"):
        logger.info("=" * 72)
        logger.info("OTEL COLLECTOR ARTIFACT METRICS — %s", collector_label)
        logger.info("  artifact:           %s", expanded_path)
        logger.info("  file size (bytes):  %d", file_bytes)
        logger.info("  OTLP lines:         %d (json decode errors: %d)", total_lines, json_errors)
        logger.info("  unique metric names in artifact: %d", len(artifact_names))

        cap_note = (
            f" (capped at {max_metric_names} per section)"
            if max_metric_names is not None
            else " (full list)"
        )
        logger.info(
            "  ARTIFACT METRIC NAMES%s: nvswitch=%d prometheus=%d other=%d",
            cap_note,
            len(nvswitch_names),
            len(prometheus_names),
            len(other_names),
        )
        _log_sorted_metric_names(
            nvswitch_names,
            "artifact — NVUE / nvswitch telemetry",
            max_names=max_metric_names,
        )
        _log_sorted_metric_names(
            other_names,
            "artifact — other",
            max_names=max_metric_names,
        )
        _log_sorted_metric_names(
            prometheus_names,
            "artifact — prometheus sidecar",
            max_names=max_metric_names,
        )

        if parsed_metrics_timestamps is not None:
            only_artifact = sorted(artifact_names - parsed_names)
            only_parsed = sorted(parsed_names - artifact_names)
            logger.info(
                "  parsed cache metric count: %d (artifact=%d)",
                len(parsed_names),
                len(artifact_names),
            )
            _log_sorted_metric_names(
                only_artifact,
                "in artifact only (not in parsed cache)",
                max_names=max_metric_names,
            )
            _log_sorted_metric_names(
                only_parsed,
                "in parsed cache only (not in artifact scan)",
                max_names=max_metric_names,
            )

        logger.info("=" * 72)
        try:
            allure.attach(
                f"otel-artifact-metrics-{collector_label}",
                full_metric_list_text,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("OTEL: Allure attach of artifact metrics failed: %s", exc)


def attach_otel_artifact_debug(
    artifact_path: str,
    hostname: str,
    *,
    max_lines: int = 5,
    max_line_chars: int = 20_000,
) -> None:
    """Attach a small raw OTLP preview and resource attributes for parser debugging."""
    expanded_path = os.path.expanduser(artifact_path)
    raw_preview: List[str] = []
    resource_attrs: List[Any] = []
    total_lines = 0
    hostname_seen = False

    if not os.path.isfile(expanded_path):
        allure.attach(
            "otel-artifact-debug",
            json.dumps(
                {
                    "artifact": expanded_path,
                    "exists": False,
                    "expected_hostname": hostname,
                },
                indent=2,
            ),
        )
        return

    with open(expanded_path, "r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            total_lines += 1
            if hostname and hostname in line:
                hostname_seen = True
            if len(raw_preview) < max_lines:
                raw_preview.append(line[:max_line_chars])
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for resource_metric in obj.get("resourceMetrics", []) or []:
                    attrs = resource_metric.get("resource", {}).get("attributes", []) or []
                    resource_attrs.append(attrs)

    debug_summary = {
        "artifact": expanded_path,
        "exists": True,
        "total_non_empty_lines": total_lines,
        "expected_hostname": hostname,
        "hostname_seen_anywhere": hostname_seen,
        "preview_lines": len(raw_preview),
        "resource_attributes_from_preview": resource_attrs[:20],
    }
    allure.attach("otel-artifact-debug-summary", json.dumps(debug_summary, indent=2))
    allure.attach(
        "otel-artifact-raw-preview",
        "\n".join(raw_preview) or "(artifact has no non-empty lines)",
    )


def collect_and_cache_mgmt_vrf_otel_session(
    dut,
    collector: OtelCollector,
    cur_dir: str,
    *,
    collector_ips: Tuple[str, ...],
    apply_telemetry_config: bool = True,
    wait_sec: int = CumulusOtelConst.TEST01_COLLECTION_WAIT_SEC,
    vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
    cli_platform_only: bool = False,
    cli_control_plane_only: bool = False,
    attach_parse_debug: bool = False,
    telemetry_unit: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect OTLP export + post-collection CLI and populate the telemetry cache.

    Used by Cumulus ``test01``–``test05``. Caches ``otel`` (parsed OTLP structures) and
    ``cli`` (platform, interface, histogram, and control-plane CLI snapshots).
    """
    if apply_telemetry_config:
        apply_otel_mgmt_vrf_no_tls_telemetry_config(dut, collector_ips=collector_ips)

    otel_data_val = OtelDataValidations()
    hostname = get_dut_hostname(dut)

    cp_stats_pre = None
    if not cli_platform_only:
        with allure.step("CLI: control-plane counters (pre-collection)"):
            cp_stats_pre = get_cp_stat_sx_api_cli(dut)

    truncate_collector_export(collector)
    collect_telemetry_data(
        dut,
        collector,
        vrf,
        wait_sec,
        None,
        cur_dir,
        telemetry_unit=telemetry_unit,
    )

    artifact_path = cumulus_otel_artifact_path(cur_dir)
    otel_payload = parse_telemetry_and_cache(
        otel_data_val,
        artifact_path=artifact_path,
        hostname=hostname,
        attach_debug=attach_parse_debug,
    )

    collector_label = f"{vrf} VRF primary ({hostname})"
    log_collector_export_metrics(
        artifact_path,
        collector_label=collector_label,
        parsed_metrics_timestamps=otel_payload.get("metrics_timestamps"),
    )
    log_parsed_otel_payload_summary(
        otel_payload,
        hostname=hostname,
        collector_label=collector_label,
    )

    with allure.step("Collect post-export CLI snapshots"):
        if cli_platform_only:
            cli_payload = collect_cli_platform_only(dut)
        elif cli_control_plane_only:
            cli_payload = collect_control_plane_stats_only(dut, cp_stats_pre=cp_stats_pre)
        else:
            cli_payload = collect_cli_mgmt_vrf_session_data(dut, cp_stats_pre=cp_stats_pre)
        telemetryCache.add_data("cli", cli_payload)

    log_cli_cache_summary(cli_payload, hostname=hostname)

    return {"otel": otel_payload, "cli": cli_payload}


def ensure_mgmt_vrf_otel_cli_cache(
    engines,
    otel_suite_mgmt,
    tmp_path: str,
    *,
    collector_ips: Tuple[str, ...],
    apply_telemetry_config: bool = True,
    cli_platform_only: bool = False,
) -> None:
    """Populate ``otel`` and ``cli`` cache entries if not already present (session reuse)."""
    if (
        telemetryCache.get_data_optional("otel") is not None and
        telemetryCache.get_data_optional("cli") is not None
    ):
        return
    collect_and_cache_mgmt_vrf_otel_session(
        engines.dut,
        otel_suite_mgmt.primary,
        tmp_path,
        collector_ips=collector_ips,
        apply_telemetry_config=apply_telemetry_config,
        cli_platform_only=cli_platform_only,
    )


def ensure_platform_stats_otel_cli_cache(
    engines,
    otel_suite,
    tmp_path: str,
    *,
    collector_ips: Tuple[str, ...],
    vrf: str,
    apply_telemetry_config: bool = False,
    telemetry_unit: Optional[str] = None,
    wait_sec: Optional[int] = None,
) -> None:
    """Populate platform-stats OTEL and CLI cache for the current DUT profile.

    The caller owns VRF/config selection. This helper only performs the common
    collect/parse/cache flow used by both NVOS/default-VRF and Cumulus/mgmt-VRF
    platform-stats validation.
    """
    if (
        telemetryCache.get_data_optional("otel") is not None and
        telemetryCache.get_data_optional("cli") is not None
    ):
        return

    if wait_sec is None:
        if apply_telemetry_config:
            wait_sec = CumulusOtelConst.TEST01_COLLECTION_WAIT_SEC
        else:
            wait_sec = OtelCollectorConst.collection_window_sec(
                OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC
            )

    collect_and_cache_mgmt_vrf_otel_session(
        engines.dut,
        otel_suite.primary,
        tmp_path,
        collector_ips=collector_ips,
        apply_telemetry_config=apply_telemetry_config,
        wait_sec=wait_sec,
        vrf=vrf,
        cli_platform_only=True,
        attach_parse_debug=True,
        telemetry_unit=telemetry_unit,
    )


def validate_cached_platform_stats_against_cli(devices) -> None:
    """Validate cached platform-stats OTEL values against cached platform CLI data."""
    otel_platform_stats = telemetryCache.get_data("otel").get("platform_stats")
    cli_data = telemetryCache.get_data("cli")
    platform_profile = "cumulus" if devices.dut.is_eth() else "nvos"

    with allure.step("Validate platform stats OTEL vs CLI (test05)"):
        OtelDataValidations().plat_stats_data_validation(
            otel_platform_stats,
            cli_data.get("plat_env_temp"),
            cli_data.get("plat_env_psu"),
            cli_data.get("plat_env_fan"),
            platform_profile=platform_profile,
        )


def ensure_control_plane_stats_otel_cli_cache(
    engines,
    otel_suite,
    tmp_path: str,
    *,
    collector_ips: Tuple[str, ...],
    vrf: str,
    apply_telemetry_config: bool = False,
    telemetry_unit: Optional[str] = None,
    wait_sec: Optional[int] = None,
) -> None:
    """Populate control-plane-stats OTEL and CLI cache for the current DUT profile.

    Mirrors :func:`ensure_platform_stats_otel_cli_cache` but keeps the full CLI
    payload (``cli_platform_only=False``) so the ``sx_api`` control-plane pre/post
    snapshots (cache key ``cp_stats``) are captured for test04 validation.
    """
    # Content-aware skip: a prior platform-only collection populates ``otel``/``cli``
    # without ``cp_stats`` (it uses ``cli_platform_only=True``). Returning on mere
    # presence would leave test04 validation with no cp_stats to check, so only skip
    # re-collection when both cache entries actually carry ``cp_stats``.
    cached_otel = telemetryCache.get_data_optional("otel") or {}
    cached_cli = telemetryCache.get_data_optional("cli") or {}
    if cached_otel.get("cp_stats") and cached_cli.get("cp_stats"):
        return

    if wait_sec is None:
        if apply_telemetry_config:
            wait_sec = CumulusOtelConst.TEST01_COLLECTION_WAIT_SEC
        else:
            wait_sec = OtelCollectorConst.collection_window_sec(
                OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC
            )

    collect_and_cache_mgmt_vrf_otel_session(
        engines.dut,
        otel_suite.primary,
        tmp_path,
        collector_ips=collector_ips,
        apply_telemetry_config=apply_telemetry_config,
        wait_sec=wait_sec,
        vrf=vrf,
        cli_platform_only=False,
        cli_control_plane_only=True,
        attach_parse_debug=True,
        telemetry_unit=telemetry_unit,
    )


def validate_cached_control_plane_stats_against_cli(devices) -> None:
    """Validate cached control-plane OTEL values against cached ``sx_api`` CLI data (test04).

    Runs the global control-plane counters plus the per-trap and per-trap-group
    validations. Trap/group series are matched by their ``group`` / ``trap_id``
    labels regardless of the extra ``group_name`` / ``trap_name`` OTEL labels.
    """
    del devices
    otel_cp_stats = telemetryCache.get_data("otel").get("cp_stats")
    cli_cp_stats = telemetryCache.get_data("cli").get("cp_stats")
    if not otel_cp_stats or not cli_cp_stats:
        pytest.skip("No cp_stats in telemetry cache")

    otel_data_val = OtelDataValidations()
    with allure.step("Validate control-plane stats OTEL vs CLI (test04)"):
        otel_data_val.cp_stats_data_validation(otel_cp_stats, cli_cp_stats)
        cli_pre = cli_cp_stats.get("pre") or {}
        cli_post = cli_cp_stats.get("post") or {}
        trap_pre, trap_post = cli_pre.get("Trap Counters"), cli_post.get("Trap Counters")
        if trap_pre and trap_post:
            otel_data_val.traps_data_validation(otel_cp_stats, trap_pre, trap_post)
        group_pre = cli_pre.get("Group Counters")
        group_post = cli_post.get("Group Counters")
        if group_pre and group_post:
            otel_data_val.trap_groups_data_validation(otel_cp_stats, group_pre, group_post)


def get_dut_hostname(dut) -> str:
    try:
        system = OutputParsingTool.parse_show_output_to_dict(System().show()).get_returned_value()
        hostname = (system or {}).get("hostname")
        if hostname:
            return str(hostname).strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("System().show() hostname parse failed: %s", exc)
    return dut.run_cmd("hostname", validate=False, print_output=False).strip()


def parse_telemetry_and_cache(
    otel_data_val: OtelDataValidations,
    artifact_path: str,
    hostname: str,
    *,
    cache_key: str = "otel",
    include_time_gaps: bool = False,
    dut=None,
    attach_debug: bool = False,
    family_map: Optional[list] = None,
) -> Dict[str, Any]:
    with allure.step("Parse telemetry data and populate cache"):
        if attach_debug:
            attach_otel_artifact_debug(artifact_path, hostname)
        parsed_objects = otel_data_val.parse_disjoint_json(artifact_path, hostname)
        if not parsed_objects:
            if not attach_debug:
                attach_otel_artifact_debug(artifact_path, hostname)
            pytest.fail("No telemetry data collected")

        metrics_timestamps = otel_data_val.metricTimestamps(parsed_objects)
        otel_payload: Dict[str, Any] = {
            "metrics_timestamps": metrics_timestamps,
            "platform_stats": otel_data_val.metricPlatformStatsValues(parsed_objects),
            "cp_stats": otel_data_val.metricControlPlaneStatsValues(parsed_objects),
            "intf_stats": otel_data_val.metricIntfStatsValues(parsed_objects),
            "histograms": otel_data_val.metricHistogramValues(parsed_objects),
            "hist_list": otel_data_val.getHistogramList(parsed_objects),
        }
        if include_time_gaps:
            from ngts.tests_nvos.system.telemetry.otel.cumulus.sample_interval import (
                get_dut_os_info,
                timegap_analysis_v2,
            )

            os_info = get_dut_os_info(dut) if dut is not None else ""
            otel_payload["time_gaps"] = timegap_analysis_v2(
                artifact_path,
                hostname,
                os_info=os_info,
                family_map=family_map,
            )
        assert set(otel_payload.keys()).issubset(set(OTEL_CACHE_PAYLOAD_KEYS) | {"time_gaps"})
        telemetryCache.add_data(cache_key, otel_payload)
        return otel_payload


def collect_and_cache_mgmt_vrf_insecure_validation_session(
    dut,
    collector: OtelCollector,
    cur_dir: str,
    *,
    collector_ips: Tuple[str, ...],
    wait_sec: int = CumulusOtelConst.TEST01_COLLECTION_WAIT_SEC,
) -> Dict[str, Any]:
    """``Test_Otel_Mgmt_Vrf_Insecure`` pre_suite parity: OTLP + CLI for test02a–test03e.

    SSIM runs Scapy leaf2/leaf3 traffic before collection; mlx lab has no multi-leaf
    topo, so interface counter validations may see zero traffic on both CLI and OTEL.
    Histogram snapshot is best-effort (test03a–d skip when ``hist_snap`` is empty).
    """
    apply_otel_mgmt_vrf_no_tls_telemetry_config(
        dut,
        collector_ips=collector_ips,
        enable_interface_histogram=True,
    )
    otel_data_val = OtelDataValidations()
    hostname = get_dut_hostname(dut)
    cp_stats_pre = get_cp_stat_sx_api_cli(dut)

    truncate_collector_export(collector)
    vrf = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT
    collect_telemetry_data(dut, collector, vrf, wait_sec, None, cur_dir)

    artifact_path = cumulus_otel_artifact_path(cur_dir)
    otel_payload = parse_telemetry_and_cache(
        otel_data_val,
        artifact_path=artifact_path,
        hostname=hostname,
    )
    cli_payload = collect_cli_mgmt_vrf_session_data(
        dut,
        cp_stats_pre=cp_stats_pre,
        require_hist_snap=False,
    )
    telemetryCache.add_data("cli", cli_payload)
    log_cli_cache_summary(cli_payload, hostname=hostname)
    log_parsed_otel_payload_summary(
        otel_payload,
        hostname=hostname,
        collector_label="mgmt VRF insecure validation",
    )
    return {"otel": otel_payload, "cli": cli_payload}
