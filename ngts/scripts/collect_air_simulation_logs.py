"""
Collect AIR simulation plugin logs via the Air Loki proxy for bug-handler / tech-support dumps.

Programmatic access:
https://nvidia.atlassian.net/wiki/spaces/NetworkingBU/pages/2996733775/Datadog+and+Kratos+Observability#Programmatic-access-to-simulator-engine-logs

The Air team runs a transparent proxy in front of the Kratos Loki query API. No client
auth is required; the proxy injects mTLS and X-Scope-OrgID. Use standard Loki read
endpoints under /loki/api/v1/.

Optional environment:
  AIR_LOKI_PROXY_URL (default: staging Air Loki proxy)
  AIR_SIMULATION_LOG_SERVICE_NAME (default: simx/switch)
"""

import allure
import json
import logging
import os
import re
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

from devts.infra.tools.nvidia_air_tools.air import get_setup_devices_from_air

logger = logging.getLogger(__name__)

DEFAULT_PROXY_URL = "https://air-brain-deploy-loki-proxy.stg.astra.nvidia.com"
DEFAULT_SERVICE_NAME = "simx/switch"
DEFAULT_LOG_LIMIT = 3000
LOKI_QUERY_TIMEOUT_SECONDS = 120
HTTP_TOO_MANY_REQUESTS = 429
ENV_PROXY_URL = "AIR_LOKI_PROXY_URL"
ENV_SERVICE_NAME = "AIR_SIMULATION_LOG_SERVICE_NAME"


def _proxy_settings():
    return {
        "url": os.environ.get(ENV_PROXY_URL, DEFAULT_PROXY_URL).rstrip("/"),
        "service_name": os.environ.get(ENV_SERVICE_NAME, DEFAULT_SERVICE_NAME),
    }


def _escape_logql_label_value(value: str) -> str:
    """Escape a value used inside a LogQL label matcher."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_logql_query(simulation_id: str) -> str:
    simulation_id = _escape_logql_label_value(simulation_id)
    return f'{{source="app", simulation_id="{simulation_id}"}}'


def fetch_air_simulation_logs_from_loki(
    simulation_id: str,
    start: datetime,
    end: datetime,
    service_name: str | None = None,
    limit: int = DEFAULT_LOG_LIMIT,
) -> dict:
    """
    Query simulation logs through the Air Loki proxy.

    :return: merged Loki query_range JSON response (data.result contains all pages)
    """
    settings = _proxy_settings()
    service_name = service_name or settings["service_name"]
    # TODO: filter by service_name once the Air team provides a way to do so
    query = _build_logql_query(simulation_id)
    query_range_url = f"{settings['url']}/loki/api/v1/query_range"
    start_ns = int(start.timestamp() * 1e9)
    end_ns = int(end.timestamp() * 1e9)
    merged_streams = {}
    current_start_ns = start_ns

    logger.info(
        "Fetching AIR simulation logs from Loki proxy: simulation_id=%s service_name=%s "
        "url=%s start=%s end=%s",
        simulation_id,
        service_name,
        settings["url"],
        start.isoformat(),
        end.isoformat(),
    )

    page = 0
    while current_start_ns < end_ns:
        page += 1
        params = {
            "query": query,
            "start": str(current_start_ns),
            "end": str(end_ns),
            "limit": limit,
            "direction": "forward",
        }
        response = requests.get(query_range_url, params=params, timeout=LOKI_QUERY_TIMEOUT_SECONDS)
        if response.status_code == HTTP_TOO_MANY_REQUESTS:
            raise RuntimeError(
                "Loki query API rate limited (HTTP 429). Reach out in #nv-kratos for higher limits."
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"Loki query failed: {payload}")

        page_results = payload.get("data", {}).get("result", [])
        logger.info("Loki page %d: %d stream(s) fetched from ts_ns=%s", page, len(page_results), current_start_ns)
        if not page_results:
            break

        last_ts_ns = _merge_page_results(merged_streams, page_results, current_start_ns)

        if last_ts_ns >= end_ns:
            break
        # Loki 'start' is inclusive; step 1ns(nano-second) past the last line so we don't re-fetch it.
        current_start_ns = last_ts_ns + 1

    return {
        "status": "success",
        "data": {"result": list(merged_streams.values())},
    }


def _merge_page_results(merged_streams: dict, page_results: list, current_start_ns: int) -> int:
    """
    Merge a page of Loki streams into ``merged_streams`` in place.

    :return: the latest log timestamp (ns) seen in this page (or ``current_start_ns`` if none)
    """
    last_ts_ns = current_start_ns
    for stream in page_results:
        stream_labels = stream.get("stream", {})
        label_key = tuple(sorted(stream_labels.items()))
        merged_streams.setdefault(label_key, {"stream": stream_labels, "values": []})
        for ts_ns, log_line in stream.get("values", []):
            merged_streams[label_key]["values"].append((ts_ns, log_line))
            last_ts_ns = max(last_ts_ns, int(ts_ns))
    return last_ts_ns


def _extract_air_log_entry(log_line: str) -> dict | None:
    """Parse nested AIR/Kratos JSON log line into the inner message dict."""
    try:
        outer = json.loads(log_line)
    except json.JSONDecodeError:
        return None
    message = outer.get("message", outer)
    if isinstance(message, str):
        try:
            message = json.loads(message)
        except json.JSONDecodeError:
            return None
    return message if isinstance(message, dict) else None


def format_loki_query_response(payload: dict, service_name: str | None = None) -> str:
    """Turn Loki query_range JSON into a plain-text log file."""
    lines = []
    for stream in payload.get("data", {}).get("result", []):
        stream_labels = stream.get("stream", {})
        label_str = ", ".join(f'{k}="{v}"' for k, v in sorted(stream_labels.items()))
        lines.append(f"--- stream: {label_str} ---")
        for ts_ns, log_line in stream.get("values", []):
            message = _extract_air_log_entry(log_line)
            if service_name and message and message.get("service_name") != service_name:
                continue
            try:
                ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc).isoformat()
            except (TypeError, ValueError):
                ts = str(ts_ns)
            if message:
                node_name = message.get("node_name", "")
                log_text = message.get("log", log_line)
                lines.append(f"{ts} [{node_name}] {log_text}".rstrip())
            else:
                lines.append(f"{ts} {log_line}")
        # blank line to visually separate one stream's block from the next in the output file
        lines.append("")
    if not lines:
        lines.append("(no log lines returned for the given time range and filters)")
    return "\n".join(lines)


def _build_output_path(dumps_folder: str, test_name: str, simulation_id: str):
    safe_test_name = re.sub(r"[^\w.\-]", "_", os.path.basename(test_name.replace("/", "_")))
    safe_simulation_id = re.sub(r"[^\w\-]", "_", simulation_id)
    dumps_root = Path(dumps_folder).resolve()
    dest_path = (dumps_root / f"{safe_test_name}_air_simulation_{safe_simulation_id}_logs.txt").resolve()
    if dumps_root not in dest_path.parents and dest_path != dumps_root:
        raise ValueError(f"Refusing to write AIR simulation logs outside {dumps_root}: {dest_path}")
    return str(dest_path)


def collect_air_simulation_logs(
    setup_name: str,
    dumps_folder: str,
    test_name: str,
    duration_seconds: int,
    service_name: str | None = None,
) -> str | None:
    """
    Fetch simulator(SimX) logs from the Air Loki proxy and write them to a text file under ``dumps_folder``.

    :return: path to the written log file, or None on failure
    """
    settings = _proxy_settings()
    _, simulation = get_setup_devices_from_air(setup_name)
    simulation_id = simulation.id
    logger.info("Resolved simulation_id=%s for setup %s", simulation_id, setup_name)
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=max(duration_seconds, 60))

    payload = fetch_air_simulation_logs_from_loki(
        simulation_id=simulation_id,
        start=start,
        end=end,
        service_name=service_name,
    )
    dest_path = _build_output_path(dumps_folder, test_name, simulation_id)
    with allure.step(f"Write AIR simulation logs to file {dest_path}"):
        with open(dest_path, "w", encoding="utf-8") as out_file:
            out_file.write(f"# simulation_id={simulation_id}\n")
            out_file.write(f"# setup_name={setup_name}\n")
            out_file.write(f"# proxy_url={settings['url']}\n")
            out_file.write(f"# start={start.isoformat()}\n")
            out_file.write(f"# end={end.isoformat()}\n")
            out_file.write(format_loki_query_response(payload, service_name=service_name))
            out_file.write("\n")

    logger.info("AIR simulation logs written to %s", dest_path)
    return dest_path
