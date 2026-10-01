"""Remote-control wrapper for an OTel collector (primary on engines.sonic_mgmt; secondary on engines.ha).

Both collectors share the same install/run/stop code path:
- Install: download upstream `.tar.gz`, drop the binary into `/usr/local/bin`.
- Run:     foreground process under `nohup`, log file only (no pidfile).
- Stop:    SIGTERM all ``otelcol-contrib`` on the host, then by signature; SIGKILL survivors.
- Identify: ``pgrep -f signature`` — never goes stale, no pidfile needed.

All shell commands are issued with a ``sudo `` prefix — uniform across both labels.
On hosts where the SSH user is already root (e.g. the sonic-mgmt container) ``sudo``
is a no-op; on hosts where it isn't (HA), it elevates as needed for /etc/otelcol/
and /usr/local/bin writes. The two instances are distinguished only by per-host
filesystem paths so they can coexist on the same host without colliding.
"""

import logging
import os
import re
import shlex
import time
from typing import Dict, Optional, Set

import pytest
import yaml

import ngts.tools.test_utils.allure_utils as allure
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.linux_tools.linux_tools import scp_file

from ngts.tests_nvos.system.telemetry.otel.constants import OtelCollectorConst, OtelCollectorLabel

logger = logging.getLogger(__name__)


def _normalize_config(text: str) -> str:
    """Strip per-line trailing whitespace and CRs so remote/local YAML compare cleanly."""
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines())


class OtelCollector:
    """OTel collector remote-control handle. Identical lifecycle for primary and secondary instances."""

    def __init__(
        self,
        engine: LinuxSshEngine,
        label: OtelCollectorLabel,
        *,
        ip: str,
        config_path: str,
        output_json_path: str,
        config_yaml: str,
        version: str,
        log_path: str,
        staged_output_json_path: str,
        sudo_prefix: str = "sudo ",
        output_json_rotated_glob: Optional[str] = None,
        binary_name: str = OtelCollectorConst.BINARY_NAME,
    ):
        self.engine = engine
        self.label = label
        self._ip = ip
        self.binary_name = binary_name
        self.config_path = config_path
        self.output_json_path = output_json_path
        self.output_json_rotated_glob = output_json_rotated_glob
        self.config_yaml = config_yaml
        self.version = version
        self.log_path = log_path
        self.staged_output_json_path = staged_output_json_path
        self._sudo_prefix = sudo_prefix

    @classmethod
    def build_collector(cls, engines, label: OtelCollectorLabel) -> "OtelCollector":
        """Build a collector for the given suite role.

        ``label`` selects both the ``engines.*`` host (see
        ``OtelCollectorConst.HOST_ATTR_BY_LABEL``) and the matching
        ``OtelCollectorConst.{PRIMARY,SECONDARY}_*`` bundle (paths, config yaml,
        log/pid files). Fails (does not skip) if the required engine is missing
        from the topology.
        """
        host_attr = OtelCollectorConst.HOST_ATTR_BY_LABEL[label]
        if not hasattr(engines, host_attr):
            pytest.fail(
                f"OTEL {label.value} collector requires engines.{host_attr}; "
                f"topology has no '{host_attr.replace('_', '-')}' player."
            )
        engine = getattr(engines, host_attr)
        # PRIMARY_* / SECONDARY_* constants are paired by naming convention with the enum
        # member name (see comment block in constants.py). Resolve via getattr so this
        # method stays one body for both labels.
        prefix = label.name
        return cls(
            engine=engine,
            label=label,
            ip=engine.ip,
            config_path=getattr(OtelCollectorConst, f"{prefix}_CONFIG_PATH"),
            output_json_path=getattr(OtelCollectorConst, f"{prefix}_OUTPUT_JSON_PATH"),
            output_json_rotated_glob=getattr(OtelCollectorConst, f"{prefix}_OUTPUT_JSON_ROTATED_GLOB"),
            config_yaml=getattr(OtelCollectorConst, f"{prefix}_CONFIG_YAML"),
            version=OtelCollectorConst.OTEL_COLLECTOR_VERSION,
            log_path=getattr(OtelCollectorConst, f"{prefix}_LOG_PATH"),
            staged_output_json_path=getattr(OtelCollectorConst, f"{prefix}_STAGED_OUTPUT_JSON_PATH"),
        )

    @property
    def ip(self) -> str:
        return self._ip

    def ensure_running(self, install_if_missing: bool = True) -> None:
        """Install (if needed), write the minimal config, (re)start, wait until listening."""
        with allure.step(f"Ensure {self.binary_name} running ({self.label.value})"):
            self._ensure_installed(install_if_missing=install_if_missing)
            self._write_config()
            self._restart_collector_process()
            self._wait_until_listening()

    def ensure_config_applied(self, install_if_missing: bool = False) -> bool:
        """Rewrite + restart only when the live collector drifted from ``config_yaml``.

        The insecure and secured suites build separate objects over the same host,
        config path and port, so whichever suite initializes last owns the running
        receiver. A secured test running after an insecure one must restore its own
        TLS receiver or the DUT handshake fails against a plaintext listener.

        Returns True when the collector had to be reconfigured.
        """
        remote_config = self.engine.run_cmd(
            f"{self._sudo_prefix}cat {shlex.quote(self.config_path)}",
            validate=False,
            print_output=False,
        )
        # Containment, not equality: run_cmd may prepend a command echo, and line
        # endings differ across the collector hosts.
        expected = _normalize_config(self.config_yaml)
        if expected and expected in _normalize_config(remote_config) and self.is_server_active():
            return False
        with allure.step(f"Restore {self.label.value} collector config (drifted)"):
            self.ensure_running(install_if_missing=install_if_missing)
        return True

    def is_server_active(self) -> bool:
        """True when this collector process is running (SSIM ``is_otelcol_server_active``)."""
        return self._collector_process_active()

    def stop(self) -> None:
        """Stop the collector foreground process (SIGTERM, then SIGKILL fallback)."""
        with allure.step(f"Stop {self.label.value} collector process"):
            self._stop_collector_process(ignore_missing=True)

    def truncate_artifact(self) -> None:
        """Empty the live artifact and remove any stale rotated files.

        Removing the rotated files matters because :meth:`_latest_artifact_remote_path`
        falls back to the rotated glob when the live file is empty; without this rm a
        leftover rotated file from a prior run would be picked up immediately and the
        test would parse stale metrics.
        """
        path = self.output_json_path
        rotated_glob = self.output_json_rotated_glob
        sudo = self._sudo_prefix
        with allure.step(f"Truncate live artifact ({path}) and remove stale rotated"):
            # ``truncate -s 0`` empties the live file in-place (auto-creates if missing,
            # so no pre-mkdir needed). The rotated glob stays UNQUOTED so the remote
            # shell can expand it. Trailing ``true`` keeps overall exit 0 regardless of
            # whether either file existed (a fresh run has neither).
            cmd = f"{sudo}truncate -s 0 {shlex.quote(path)} 2>/dev/null; "
            if rotated_glob and rotated_glob != path:
                cmd += f"{sudo}rm -f {rotated_glob} 2>/dev/null; "
            cmd += "true"
            self.engine.run_cmd(cmd, validate=False)

    def wait_for_artifact(
        self,
        timeout_sec: int = OtelCollectorConst.ARTIFACT_TIMEOUT_SEC,
        retry_interval_sec: int = OtelCollectorConst.START_RETRY_INTERVAL_SEC,
    ) -> str:
        """Wait until a non-empty file exporter artifact appears; return its remote path."""
        with allure.step(f"Wait for non-empty collector artifact (timeout={timeout_sec}s)"):
            deadline = time.time() + timeout_sec
            attempt = 0
            while time.time() < deadline:
                attempt += 1
                latest = self._latest_artifact_remote_path(poll_attempt=attempt)
                if latest:
                    logger.info(
                        "OTEL artifact ready host=%s label=%s path=%r attempt=%d",
                        self.engine.ip,
                        self.label.value,
                        latest,
                        attempt,
                    )
                    return latest
                time.sleep(retry_interval_sec)
            probe = self._probe_artifact_on_collector(poll_attempt=attempt, final=True)
            self._maybe_artifact_breakpoint("wait_for_artifact_timeout")
        pytest.fail(
            f"OTEL output artifact not found within {timeout_sec}s "
            f"({self.output_json_path}, {self.output_json_rotated_glob}). "
            f"Collector probe:\n{probe[-3000:]}"
        )

    def fetch_artifact(
        self,
        local_dir: str,
        file_name: str = "otel-out.json",
        *,
        timeout_sec: int = OtelCollectorConst.ARTIFACT_TIMEOUT_SEC,
    ) -> str:
        """Stage the latest non-empty artifact and SCP it locally."""
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, file_name)
        with allure.step(
            f"Identify latest collector artifact (timeout={timeout_sec}s)"
        ):
            remote_path = self.wait_for_artifact(timeout_sec=timeout_sec)
        with allure.step("Stage collector artifact for fetch"):
            staged_remote = self._stage_for_fetch(remote_path)
        with allure.step(f"SCP collector artifact to {local_path}"):
            scp_file(self.engine, staged_remote, local_path, download_from_remote=True)
        return local_path

    def fetch_artifact_if_any(
        self,
        local_dir: str,
        file_name: str = "otel-out.json",
    ) -> Optional[str]:
        """Stage and SCP the latest non-empty artifact, or return ``None`` when there is none.

        The tolerant counterpart of :meth:`fetch_artifact`, which polls until data
        arrives and fails when it never does. Once the DUT export is disabled the
        file exporter writes nothing at all, so "no artifact" is the expected
        outcome and has to be reported instead of polled for until timeout.

        Checked once rather than polled: callers truncate before their collection
        window, so a single look afterwards sees anything written during it.
        """
        with allure.step("Check whether the collector wrote an artifact at all"):
            remote_path = self._latest_artifact_remote_path()
        if not remote_path:
            logger.info(
                "No non-empty OTEL artifact host=%s label=%s live=%s rotated=%s",
                self.engine.ip,
                self.label.value,
                self.output_json_path,
                self.output_json_rotated_glob,
            )
            return None
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, file_name)
        with allure.step("Stage collector artifact for fetch"):
            staged_remote = self._stage_for_fetch(remote_path)
        with allure.step(f"SCP collector artifact to {local_path}"):
            scp_file(self.engine, staged_remote, local_path, download_from_remote=True)
        return local_path

    def _ensure_installed(self, install_if_missing: bool) -> None:
        with allure.step(f"Check {self.binary_name} is installed"):
            out = self.engine.run_cmd(f"command -v {self.binary_name}", validate=False).strip()
        if out:
            return
        if not install_if_missing:
            pytest.fail(f"{self.binary_name} is not installed on {self.engine.ip}")
        self._install_from_tarball()
        with allure.step(f"Verify {self.binary_name} installed after install attempt"):
            out = self.engine.run_cmd(f"command -v {self.binary_name}", validate=False).strip()
        if not out:
            pytest.fail(f"failed to install {self.binary_name} on {self.engine.ip}")
        logger.info("installed %s on %s at %s", self.binary_name, self.engine.ip, out)

    def _install_from_tarball(self) -> None:
        """Install otelcol-contrib from the upstream .tar.gz into /usr/local/bin.

        Sub-steps wrapped in their own ``allure.step`` so failures pinpoint exactly
        which operation went wrong (download / extract / install).
        """
        archive = f"otelcol-contrib_{self.version}_linux_amd64.tar.gz"
        url = f"{OtelCollectorConst.OTEL_COLLECTOR_CONTRIB_GITHUB_RELEASES_BASE}/v{self.version}/{archive}"
        sudo = self._sudo_prefix
        dest = f"/usr/local/bin/{self.binary_name}"
        with allure.step(f"Install {self.binary_name} v{self.version} from tarball ({self.label.value})"):
            tmp = self.engine.run_cmd("mktemp -d").strip()
            if not tmp:
                pytest.fail(f"failed to create temp dir for {self.binary_name} install")
            archive_path = f"{tmp}/{archive}"
            try:
                with allure.step(f"Download tarball ({url})"):
                    # curl -fsSL: fail on HTTP errors, silent, show errors, follow redirects.
                    self.engine.run_cmd(f"curl -fsSL -o {shlex.quote(archive_path)} {shlex.quote(url)}")
                with allure.step(f"Extract {self.binary_name} from tarball"):
                    # Extract only the binary; skip LICENSE/README that ship alongside it.
                    self.engine.run_cmd(
                        f"tar -xzf {shlex.quote(archive_path)} -C {shlex.quote(tmp)} {shlex.quote(self.binary_name)}"
                    )
                with allure.step(f"Install binary to {dest}"):
                    # ``install -m 0755`` = copy + chmod rwxr-xr-x atomically.
                    self.engine.run_cmd(
                        f"{sudo}install -m 0755 {shlex.quote(f'{tmp}/{self.binary_name}')} {shlex.quote(dest)}"
                    )
            finally:
                self.engine.run_cmd(f"rm -rf {shlex.quote(tmp)}", validate=False)

    def _write_config(self) -> None:
        path = self.config_path
        sudo = self._sudo_prefix
        parent_dir = shlex.quote(os.path.dirname(path))
        with allure.step(f"Write collector config ({path})"):
            # Write the YAML over SSH using a heredoc piped through ``tee`` so we can
            # use ``sudo`` to land the file under /etc/. The ``<<'EOF'`` delimiter is
            # SINGLE-QUOTED on purpose: it disables shell expansion inside the heredoc
            # body, so the YAML is written verbatim.
            # ``set -euo pipefail`` makes any sub-step (mkdir, tee) failure abort fast.
            inner = (
                f"set -euo pipefail; "
                f"{sudo}mkdir -p {parent_dir}; "
                f"{sudo}tee {shlex.quote(path)} > /dev/null <<'EOF'\n"
                f"{self.config_yaml}\n"
                "EOF"
            )
            self.engine.run_cmd("bash -lc " + shlex.quote(inner))
        out_path = self.output_json_path
        out_parent = shlex.quote(os.path.dirname(out_path))
        with allure.step(f"Prepare file-exporter output ({out_path})"):
            # Pre-create the output file world-writable (``chmod 666``) so the
            # collector can write to it regardless of which user it runs as (root on
            # HA, SSH user inside the sonic-mgmt container). Without this, a
            # collector launched as a non-owner of /etc/otelcol/ would fail to open
            # the file for append on first emit.
            self.engine.run_cmd(
                "bash -lc " +
                shlex.quote(
                    f"{sudo}mkdir -p {out_parent}; "
                    f"{sudo}touch {shlex.quote(out_path)}; "
                    f"{sudo}chmod 666 {shlex.quote(out_path)}"
                )
            )

    def _collector_process_signature(self) -> str:
        """Unique pgrep/pkill signature for this collector instance (binary + this config)."""
        return f"{self.binary_name} --config={self.config_path}"

    def _stop_all_otelcol_processes_on_host(self) -> None:
        """Stop every ``otelcol-contrib`` on this host.

        Stale runs (e.g. ``--config=/etc/otelcol/config.yaml``) are not matched by
        our signature-based pkill but still bind the OTLP receiver ports and
        internal :8888.
        """
        sudo = self._sudo_prefix
        pattern = shlex.quote(self.binary_name)
        with allure.step(f"Stop all {self.binary_name} on {self.engine.ip} (clear stale listeners)"):
            self.engine.run_cmd(
                f"{sudo}pkill -TERM -f {pattern} 2>/dev/null || true",
                validate=False,
            )
            time.sleep(2)
            self.engine.run_cmd(
                f"{sudo}pkill -KILL -f {pattern} 2>/dev/null || true",
                validate=False,
            )
            time.sleep(1)

    def _our_collector_pids(self) -> Set[str]:
        signature = self._collector_process_signature()
        out = self.engine.run_cmd(
            f"{self._sudo_prefix}pgrep -f {shlex.quote(signature)}", validate=False
        ).strip()
        return set(out.split()) if out else set()

    def _pid_listening_on_otlp_port(
        self, port: int = OtelCollectorConst.OTLP_GRPC_PORT
    ) -> Optional[str]:
        inner = f"ss -lntp 2>/dev/null | grep ':{port}' || true"
        out = self.engine.run_cmd("bash -lc " + shlex.quote(inner), validate=False)
        match = re.search(r"pid=(\d+)", out)
        return match.group(1) if match else None

    def _configured_otlp_ports(self) -> Set[int]:
        """OTLP receiver ports defined by the config this collector is running.

        Read from the config in force rather than from
        ``OTLP_PORT_BY_TRANSPORT``, because not every config runs every receiver:
        the metrics suites serve both transports, while the syslog suites swap in
        a logs pipeline that keeps gRPC alone. Waiting on a port the running
        config never binds would hang start-up on exactly those suites.

        An unreadable config falls back to the gRPC port, the one receiver every
        config in this tree defines.
        """
        default = {OtelCollectorConst.OTLP_GRPC_PORT}
        try:
            protocols = yaml.safe_load(self.config_yaml)["receivers"]["otlp"]["protocols"]
            ports = {
                int(str(protocol["endpoint"]).rsplit(":", 1)[1])
                for protocol in protocols.values()
            }
        except (yaml.YAMLError, AttributeError, IndexError, KeyError, TypeError, ValueError):
            logger.warning(
                "Could not read the OTLP receiver ports out of the %s collector "
                "config; treating :%s as the only expected listener",
                self.label.value,
                OtelCollectorConst.OTLP_GRPC_PORT,
            )
            return default
        return ports or default

    def _otlp_ports_not_ours(self) -> Dict[int, Optional[str]]:
        """Receiver ports this collector does not own, mapped to the holding pid.

        Readiness means every receiver the running config declares is up and
        ours. A port held by a foreign pid is reported with that pid rather than
        as a plain absence, which is what tells a stale collector apart from one
        that has not finished starting.
        """
        our_pids = self._our_collector_pids()
        not_ours: Dict[int, Optional[str]] = {}
        for port in self._configured_otlp_ports():
            listener_pid = self._pid_listening_on_otlp_port(port)
            if listener_pid not in our_pids:
                not_ours[port] = listener_pid
        return not_ours

    def _restart_collector_process(self) -> None:
        sudo = self._sudo_prefix
        log_dir = os.path.dirname(self.log_path)
        with allure.step(f"Restart {self.label.value} collector process"):
            self._stop_all_otelcol_processes_on_host()
            self._stop_collector_process(ignore_missing=True)
            with allure.step("Verify collector config file is non-empty"):
                # ``test -s FILE`` => file exists AND has size > 0. Catches a
                # config-write that silently produced an empty file.
                self.engine.run_cmd(f"{sudo}test -s {shlex.quote(self.config_path)}")
            with allure.step(f"Ensure collector log directory exists ({log_dir})"):
                self.engine.run_cmd(f"mkdir -p {shlex.quote(log_dir)}", validate=False)
            with allure.step(f"Launch {self.binary_name} via nohup (config={self.config_path})"):
                # ``nohup ... &`` detaches the process from this SSH session so it
                # survives after run_cmd returns. ``> LOG 2>&1`` merges stdout+stderr
                # into the log file. We identify the running process later via
                # ``pgrep -f signature``, so no pidfile is needed.
                self.engine.run_cmd(
                    f"bash -lc 'nohup {sudo}{self.binary_name} "
                    f"--config={shlex.quote(self.config_path)} "
                    f"> {shlex.quote(self.log_path)} 2>&1 &'"
                )

    def _stop_collector_process(self, ignore_missing: bool = True) -> None:
        """Stop the collector: SIGTERM by signature, brief grace, SIGKILL survivors.

        Identification is signature-based (``pgrep -f BINARY --config=PATH``), which
        pinpoints THIS collector instance without ever going stale — so no pidfile.
        Trailing ``|| true`` on each pkill so "no process matches" (i.e. already
        stopped) is treated as success, not as an error.
        """
        signature = self._collector_process_signature()
        sudo = self._sudo_prefix
        with allure.step(f"Stop {self.label.value} collector ({self.binary_name})"):
            with allure.step("SIGTERM by signature"):
                self.engine.run_cmd(
                    f"{sudo}pkill -TERM -f {shlex.quote(signature)} 2>/dev/null || true",
                    validate=False,
                )
            time.sleep(1)  # generous grace window for clean shutdown
            with allure.step("SIGKILL any survivors by signature"):
                self.engine.run_cmd(
                    f"{sudo}pkill -KILL -f {shlex.quote(signature)} 2>/dev/null || true",
                    validate=False,
                )
        if not ignore_missing and self._collector_process_active():
            pytest.fail(f"failed to stop {self.binary_name}")

    def _collector_process_active(self) -> bool:
        signature = self._collector_process_signature()
        with allure.step(f"Check {self.label.value} collector process is active ({signature})"):
            # ``pgrep -f PATTERN`` matches against the FULL command line (binary + args),
            # not just the basename, so the signature pinpoints THIS collector instance
            # even when multiple otel collectors run on the same host. Non-zero exit when no
            # match — ``validate=False`` so that's treated as "not running", not error.
            out = self.engine.run_cmd(
                f"{self._sudo_prefix}pgrep -f {shlex.quote(signature)}", validate=False
            ).strip()
        return bool(out)

    def _wait_until_listening(self) -> None:
        timeout_sec = OtelCollectorConst.START_TIMEOUT_SEC
        retry_interval = OtelCollectorConst.START_RETRY_INTERVAL_SEC
        ports = sorted(self._configured_otlp_ports())
        port_list = ", ".join(f":{p}" for p in ports)

        with allure.step(f"Wait for OTLP listeners on {port_list}"):
            deadline = time.time() + timeout_sec
            # Probe for a listening TCP socket on the OTLP port. Prefer modern
            # ``ss -lntp`` (iproute2: -l listening, -n numeric, -t TCP, -p show
            # owning process), fall back to legacy ``netstat -ltn`` on hosts where
            # ``ss`` is unavailable. ``2>/dev/null`` silences "command not found"
            # for the missing tool; trailing ``|| true`` forces exit 0 so we read
            # empty stdout (= "not listening yet") instead of raising on no-match.
            while time.time() < deadline:
                if not self._collector_process_active():
                    time.sleep(retry_interval)
                    continue
                not_ours = self._otlp_ports_not_ours()
                if not not_ours:
                    return
                foreign = {p: pid for p, pid in not_ours.items() if pid}
                if foreign:
                    logger.warning(
                        "OTLP ports held by foreign pids %s (our pids=%s on %s); waiting",
                        foreign,
                        sorted(self._our_collector_pids()),
                        self.engine.ip,
                    )
                time.sleep(retry_interval)
        not_ours = self._otlp_ports_not_ours()
        ours = sorted(self._our_collector_pids())
        pgrep_all = self.engine.run_cmd(
            f"bash -lc {shlex.quote(f'pgrep -af {self.binary_name} || true')}",
            validate=False,
        ).strip()
        pytest.fail(
            f"{self.binary_name} did not own OTLP {port_list} within {timeout_sec}s on "
            f"{self.engine.ip} (port -> listener pid, null means no listener: "
            f"{not_ours}, our_pids={ours}). Processes: {pgrep_all!r}"
        )

    @staticmethod
    def _artifact_debug_enabled() -> bool:
        return os.environ.get("OTEL_ARTIFACT_DEBUG", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )

    def _maybe_artifact_breakpoint(self, reason: str) -> None:
        if not self._artifact_debug_enabled():
            return
        logger.warning(
            "OTEL_ARTIFACT_DEBUG: breakpoint (%s) label=%s host=%s path=%s",
            reason,
            self.label.value,
            self.engine.ip,
            self.output_json_path,
        )
        breakpoint()  # noqa: T100

    def _probe_artifact_on_collector(self, *, poll_attempt: int = 0, final: bool = False) -> str:
        """Run explicit remote checks on the configured exporter file (e.g. primary-test.json)."""
        sudo = self._sudo_prefix
        live = self.output_json_path
        base_dir = os.path.dirname(live)
        tag = "FINAL" if final else f"poll-{poll_attempt}"
        report_lines = [
            f"=== OTEL_ARTIFACT_PROBE [{tag}] ===",
            f"label={self.label.value}",
            f"host={self.engine.ip}",
            f"live_artifact_path={live}",
            f"rotated_glob={self.output_json_rotated_glob}",
            f"config_path={self.config_path}",
            f"log_path={self.log_path}",
        ]
        probes = (
            ("ls_etc_otelcol", f"{sudo}ls -la {shlex.quote(base_dir)} 2>&1"),
            ("stat_live_artifact", f"{sudo}stat {shlex.quote(live)} 2>&1"),
            (
                "wc_bytes_live_artifact",
                f"bash -lc '{sudo}wc -c < {shlex.quote(live)} 2>&1 || echo FILE_MISSING'",
            ),
            (
                "test_s_live_artifact",
                f"bash -lc '{sudo}test -s {shlex.quote(live)}; echo test_s_exit=$?'",
            ),
            (
                "head_live_artifact",
                f"bash -lc '{sudo}head -c 120 {shlex.quote(live)} 2>&1 | od -An -tx1 | head -3 || true'",
            ),
            (
                "ls_rotated_artifacts",
                f"{sudo}ls -la {self.output_json_rotated_glob} 2>&1 || true",
            ),
            (
                "grep_file_exporter_path_in_config",
                f"{sudo}grep -n 'path:' {shlex.quote(self.config_path)} 2>&1 || true",
            ),
            (
                "otlp_listeners",
                "bash -lc \"ss -lntp 2>/dev/null | grep -E '{}' || true\"".format(
                    "|".join(f":{p}" for p in sorted(self._configured_otlp_ports()))
                ),
            ),
            (
                "collector_process",
                f"bash -lc \"pgrep -af {shlex.quote(self.binary_name)} || true\"",
            ),
            (
                "collector_log_tail",
                f"{sudo}tail -n 50 {shlex.quote(self.log_path)} 2>&1 || true",
            ),
        )
        for name, cmd in probes:
            try:
                raw = self.engine.run_cmd(cmd, validate=False, print_output=False)
            except Exception as exc:
                raw = f"<run_cmd exception: {exc!r}>"
            report_lines.extend(
                (
                    f"--- {name} ---",
                    f"cmd: {cmd}",
                    f"stdout_len: {len(raw)}",
                    f"stdout_repr: {raw!r}",
                )
            )
            logger.info(
                "OTEL_ARTIFACT_PROBE [%s] %s host=%s label=%s stdout_len=%d stdout=%r",
                tag,
                name,
                self.engine.ip,
                self.label.value,
                len(raw),
                raw,
            )
        report = "\n".join(report_lines)
        logger.info("OTEL_ARTIFACT_PROBE [%s] full_report:\n%s", tag, report)
        try:
            allure.attach(f"otel-artifact-probe-{self.label.value}-{tag}", report)
        except Exception as exc:
            logger.debug("OTEL: Allure attach of artifact probe failed: %s", exc)
        return report

    def _latest_artifact_remote_path(self, *, poll_attempt: int = 0) -> Optional[str]:
        sudo = self._sudo_prefix
        # Build a small shell script that returns the path of the newest non-empty
        # artifact (or empty stdout if none yet). The contract with Python is:
        #   non-empty stdout -> ready, here's the path
        #   empty stdout     -> not ready yet, caller polls again
        # That's why every branch ``exit 0`` — "not ready" is a normal poll state,
        # not an error, so we never want a non-zero shell exit to bubble up.

        # Branch 1: live file. ``test -s FILE`` is true iff the file exists AND has
        # size > 0. Prefer this branch — the live file is the freshest data.
        lines = [
            f"if {sudo}test -s {shlex.quote(self.output_json_path)}; then "
            f"echo {shlex.quote(self.output_json_path)}; "
            "exit 0; "
            "fi"
        ]
        if self.output_json_rotated_glob and self.output_json_rotated_glob != self.output_json_path:
            # Branch 2: live file empty, fall back to the most recently rotated file.
            # ``ls -1t GLOB`` lists matches newest-first (one per line, sorted by
            # mtime). The glob stays UNQUOTED so the remote shell expands it.
            # Iterate newest-first and pick the first non-empty hit. Quoting "$f"
            # protects against unusual chars in rotation suffixes.
            lines.append(
                f"for f in $({sudo}ls -1t {self.output_json_rotated_glob} 2>/dev/null); do "
                f"if {sudo}test -s \"$f\"; then echo \"$f\"; exit 0; fi; "
                "done"
            )
        # Fall-through: nothing ready yet. Empty stdout, exit 0 (see contract above).
        lines.append("exit 0")
        inner = "; ".join(lines)
        with allure.step(
            f"Locate latest non-empty artifact (live={self.output_json_path}, "
            f"rotated={self.output_json_rotated_glob})"
        ):
            raw = self.engine.run_cmd(
                "bash -lc " + shlex.quote(inner), validate=False, print_output=False
            )
            latest = raw.strip()
        logger.info(
            "OTEL_ARTIFACT_LOCATE poll=%d host=%s label=%s live=%s "
            "inner_script=%r raw_stdout=%r stripped=%r",
            poll_attempt,
            self.engine.ip,
            self.label.value,
            self.output_json_path,
            inner,
            raw,
            latest,
        )
        return latest or None

    def _stage_for_fetch(self, source_remote_path: str) -> str:
        """Copy the artifact to a stable, SCP-readable staging path under /tmp.

        Why a separate staging step instead of SCP'ing the source directly:
        - The source lives under /etc/otelcol/ which is typically root-owned; the SCP
          user usually can't read it without sudo.
        - The source filename may carry a rotation suffix that changes between runs;
          the staged path is stable and predictable.
        """
        staged = self.staged_output_json_path
        sudo = self._sudo_prefix
        with allure.step(f"Create staging directory ({os.path.dirname(staged)})"):
            # /tmp/otel-artifacts/ is world-writable on a typical Linux host, so no
            # sudo is needed for the mkdir even when the collector itself runs as root.
            self.engine.run_cmd(f"mkdir -p {shlex.quote(os.path.dirname(staged))}")
        with allure.step(f"Copy artifact to staging path ({staged})"):
            # The SOURCE under /etc/otelcol/ is owned by root, so ``sudo cp`` is what lets
            # us read it. The destination (/tmp/...) is world-writable, so the SSH user
            # can drop files there.
            self.engine.run_cmd(
                f"{sudo}cp {shlex.quote(source_remote_path)} {shlex.quote(staged)}"
            )
        with allure.step(f"chmod 644 staged artifact ({staged})"):
            # The cp above ran as root, so the staged file inherited root ownership.
            # ``chmod 644`` (rw-r--r--) makes it world-readable so the SSH user can SCP it.
            self.engine.run_cmd(f"{sudo}chmod 644 {shlex.quote(staged)}")
        return staged
