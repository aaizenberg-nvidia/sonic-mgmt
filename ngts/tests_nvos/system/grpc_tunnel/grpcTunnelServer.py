import base64
import logging
import os
import re
import signal
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_tools.infra.CmdRunner import CmdRunner
from ngts.nvos_tools.infra.EngineAdapterTool import EngineAdapterTool
from ngts.tests_nvos.system.gnmi.constants import GnmiMode


class GrpcTunnelServer:
    """
    gNMI dial-out helper: writes a gnmic tunnel-server YAML, optional TLS material,
    and runs `gnmic --config <file> --use-tunnel-server ...`.

    - ``server=None`` (default): uses local ``CmdRunner`` / subprocess on the pytest host.
    - ``server=<engine>`` (e.g. ``engines['sonic_mgmt']``): creates the YAML and runs
      ``openssl`` / ``gnmic`` on that host via ``EngineAdapterTool.run_cmd`` (same as
      ``server.run_cmd(...)`` for SSH engines). Subscribe RPCs are wrapped in GNU
      ``timeout`` so stream mode does not hang the SSH session waiting for a prompt.

    Use ``subscribe(..., keep_subscription_alive=True)`` to leave a stream running in the
    background after the first flat update line appears, then ``stop_subscription()`` when done.
    ``subscribe_first_update_seconds`` records gnmic start → first matching telemetry duration.

    The DUT must dial the collector at an address reachable from the DUT (often the
    engine's IP + tunnel listen port).

    File layout under ``/tmp`` (unless paths are overridden)::

        /tmp/<tunnel_name>_dial_out_grpc.yaml
        /tmp/<tunnel_name>_gnmic-cert.pem
        /tmp/<tunnel_name>_gnmic-key.pem
        /tmp/<tunnel_name>_gnmic_subscribe.out   # remote background subscribe log

    Call :meth:`delete` after :meth:`stop_subscription` to remove these artifacts (also
    clears the local subscribe log path when ``server=None``).
    """

    DEFAULT_TUNNEL_ADDRESS = ':57401'
    DEFAULT_TARGET_WAIT = '300s'
    # Stream subscribe runs until timeout; allow time for tunnel register + first updates.
    DEFAULT_DIAL_OUT_VALIDATE_TIMEOUT = 120

    _LOG_TUNNEL_DISCOVERED = 'tunnel server discovered target'
    _LOG_SUBSCRIBING = 'subscribing to target'
    _LOG_DIALING_TUNNEL = 'dialing tunnel connection for tunnel target'
    _LOG_GNMI_CLIENT = 'gNMI client created'
    _LOG_SUBSCRIBE_REQUEST = 'sending gNMI SubscribeRequest'

    @staticmethod
    def default_config_path(tunnel_name: str) -> str:
        return f'/tmp/{tunnel_name}_dial_out_grpc.yaml'

    @staticmethod
    def default_tls_cert_path(tunnel_name: str) -> str:
        return f'/tmp/{tunnel_name}_gnmic-cert.pem'

    @staticmethod
    def default_tls_key_path(tunnel_name: str) -> str:
        return f'/tmp/{tunnel_name}_gnmic-key.pem'

    def __init__(
        self,
        username: str,
        password: str,
        *,
        tunnel_name: str,
        server: Any = None,
        config_path: Optional[str] = None,
        dut_ip: Optional[str] = None,
        tunnel_address: str = DEFAULT_TUNNEL_ADDRESS,
        target_wait_time: str = DEFAULT_TARGET_WAIT,
        tunnel_debug: bool = True,
        log: bool = True,
        skip_verify: bool = True,
        tls_cert_file: Optional[str] = None,
        tls_key_file: Optional[str] = None,
        tls_ca_file: Optional[str] = None,
        tls_client_auth: str = '',
        gnmi_tls_ca_file: Optional[str] = None,
        gnmi_tls_cert_file: Optional[str] = None,
        gnmi_tls_key_file: Optional[str] = None,
        gnmi_target: Optional[str] = None,
        grpc_auth: bool = False,
        cmd_time: int = 30,
        print_outputs: bool = True,
    ):
        self.server = server
        self.tunnel_name = tunnel_name
        path = config_path or self.default_config_path(tunnel_name)
        self.config_path = path if self.server is not None else os.path.abspath(path)
        self.username = username
        self.password = password
        self.dut_ip = dut_ip
        self.tunnel_address = tunnel_address
        self.target_wait_time = target_wait_time
        self.tunnel_debug = tunnel_debug
        self.log = log
        self.skip_verify = skip_verify
        if tls_cert_file is None:
            self.tls_cert_file = self.default_tls_cert_path(tunnel_name)
        else:
            self.tls_cert_file = tls_cert_file
        if tls_key_file is None:
            self.tls_key_file = self.default_tls_key_path(tunnel_name)
        else:
            self.tls_key_file = tls_key_file
        self.tls_ca_file = tls_ca_file
        self.tls_client_auth = tls_client_auth
        self.gnmi_tls_ca_file = gnmi_tls_ca_file
        self.gnmi_tls_cert_file = gnmi_tls_cert_file
        self.gnmi_tls_key_file = gnmi_tls_key_file
        self.gnmi_target = gnmi_target
        self.grpc_auth = grpc_auth
        self.cmd_time = cmd_time
        self.cmd_runner = CmdRunner('GrpcTunnelServer', cmd_time, print_outputs)

        self.subscribe_first_update_seconds: Optional[float] = None
        self._subscribe_bg_out: Optional[str] = None
        self._subscribe_local_proc: Optional[subprocess.Popen] = None
        self._subscribe_local_log: Optional[str] = None

    def _server_run(self, cmd: str, timeout: Optional[int] = None) -> str:
        assert self.server is not None, 'server engine was not set'
        t = timeout if timeout is not None else self.cmd_time
        self._log(f'server run: {cmd[:200]}{"..." if len(cmd) > 200 else ""}')
        return EngineAdapterTool.run_cmd(self.server, cmd, timeout=t)

    def _remote_write_bytes(self, remote_path: str, data: bytes) -> None:
        b64 = base64.standard_b64encode(data).decode('ascii')
        code = (
            'import base64, pathlib; '
            f'p=pathlib.Path({remote_path!r}); p.parent.mkdir(parents=True, exist_ok=True); '
            f'p.write_bytes(base64.standard_b64decode({b64!r}))'
        )
        self._server_run(f'python3 -c {shlex.quote(code)}')

    def build_config_dict(self) -> Dict[str, Any]:
        cfg: Dict[str, Any] = {
            'log': self.log,
            'username': self.username,
            'password': self.password,
        }
        # SSIM ``mtls_target_*_template.yaml`` + ``grpc_auth=True``: auth-scheme lives in YAML,
        # not on the gnmic CLI (``GnmiCollectorDialOutVX`` omits ``--auth-scheme Basic``).
        if self.grpc_auth:
            cfg['auth-scheme'] = 'Basic'
        if self.skip_verify:
            cfg['skip-verify'] = True

        tunnel: Dict[str, Any] = {
            'address': self.tunnel_address,
            'target-wait-time': self.target_wait_time,
            'debug': self.tunnel_debug,
        }
        if self.tls_cert_file and self.tls_key_file:
            tls_block: Dict[str, Any] = {
                'cert-file': self.tls_cert_file,
                'key-file': self.tls_key_file,
            }
            if self.tls_ca_file:
                tls_block['ca-file'] = self.tls_ca_file
            if self.tls_client_auth:
                tls_block['client-auth'] = self.tls_client_auth
            tunnel['tls'] = tls_block

        cfg['tunnel-server'] = tunnel
        return cfg

    def write_config(self) -> str:
        with allure.step(f'write tunnel-server gnmic config {self.config_path}'):
            data = self.build_config_dict()
            text = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
            if self.server is not None:
                self._remote_write_bytes(self.config_path, text.encode('utf-8'))
            else:
                Path(self.config_path).parent.mkdir(parents=True, exist_ok=True)
                with open(self.config_path, 'w', encoding='utf-8') as f:
                    f.write(text)
            self._log(f'wrote config ({len(data)} top-level keys)')
        return self.config_path

    def generate_self_signed_tls(
        self,
        cert_path: str,
        key_path: str,
        *,
        days: int = 3650,
        subject: str = '/CN=gnmic-grpc-tunnel',
    ) -> Tuple[str, str]:
        with allure.step(f'generate self-signed TLS cert {cert_path}'):
            cert_p = Path(cert_path)
            key_p = Path(key_path)
            cmd = (
                f'openssl req -x509 -newkey rsa:2048 -nodes '
                f'-keyout {shlex.quote(key_path)} -out {shlex.quote(cert_path)} -days {days} '
                f'-subj {shlex.quote(subject)}'
            )
            if self.server is not None:
                for d in {cert_p.parent, key_p.parent}:
                    self._server_run(f'mkdir -p {shlex.quote(str(d))}')
                self._server_run(cmd)
            else:
                cert_p.parent.mkdir(parents=True, exist_ok=True)
                key_p.parent.mkdir(parents=True, exist_ok=True)
                logging.info(f'[GrpcTunnelServer] run: {cmd}')
                subprocess.run(cmd, shell=True, check=True)
        return str(cert_p), str(key_p)

    def prepare_with_tls(
        self,
        cert_path: Optional[str] = None,
        key_path: Optional[str] = None,
        *,
        regenerate: bool = False,
        **openssl_kw: Any,
    ) -> str:
        cert_path = cert_path or self.tls_cert_file
        key_path = key_path or self.tls_key_file
        assert cert_path and key_path, 'cert_path and key_path (or defaults from tunnel_name) are required'
        if self.server is not None:
            need = regenerate
            if not need:
                check = self._server_run(
                    f'test -f {shlex.quote(cert_path)} -a -f {shlex.quote(key_path)} && echo OK || true',
                    timeout=min(self.cmd_time, 120),
                )
                need = 'OK' not in check
            if need:
                self.generate_self_signed_tls(cert_path, key_path, **openssl_kw)
        elif regenerate or not (os.path.isfile(cert_path) and os.path.isfile(key_path)):
            self.generate_self_signed_tls(cert_path, key_path, **openssl_kw)
        self.tls_cert_file = cert_path
        self.tls_key_file = key_path
        return self.write_config()

    @staticmethod
    def listen_port(tunnel_address: str) -> int:
        m = re.search(r':(\d+)\s*$', tunnel_address.strip())
        assert m, f'cannot parse listen port from tunnel address: {tunnel_address!r}'
        return int(m.group(1))

    def collector_endpoint(self, reachable_host_ip: str) -> str:
        port = self.listen_port(self.tunnel_address)
        if ':' in reachable_host_ip.strip('[]'):
            return f'[{reachable_host_ip}]:{port}'
        return f'{reachable_host_ip}:{port}'

    def dut_collector_endpoint(self, collector_ip: Optional[str] = None) -> str:
        """Address:port the DUT should dial (collector reachable from the DUT)."""
        ip = collector_ip or self.dut_ip
        assert ip, 'pass collector_ip= or set dut_ip= in the constructor'
        return self.collector_endpoint(ip)

    def gnmi_mtls_cli_suffix(self) -> str:
        """SSIM ``GnmiCollectorDialOutVX`` mtls parity for tunneled gnmi-server (:9339)."""
        if not (self.gnmi_tls_ca_file and self.gnmi_tls_cert_file and self.gnmi_tls_key_file):
            return ''
        suffix = ' --tls-ca %s --tls-cert %s --tls-key %s' % (
            shlex.quote(self.gnmi_tls_ca_file),
            shlex.quote(self.gnmi_tls_cert_file),
            shlex.quote(self.gnmi_tls_key_file),
        )
        # SSIM grpc_auth=True (all dial-out coexistence suites): omit --auth-scheme Basic;
        # username/password from gnmic YAML are used as gRPC metadata instead.
        if not self.grpc_auth:
            suffix += ' --auth-scheme Basic'
        return suffix

    def _gnmic_base(self) -> str:
        """
        Use config basename plus ``cd`` into its directory so the CLI matches::

            gnmic --config testing_dial_out_grpc.yaml --use-tunnel-server ...
        """
        conf_dir = os.path.dirname(self.config_path)
        conf_name = os.path.basename(self.config_path)
        inner = f'gnmic --config {shlex.quote(conf_name)} --use-tunnel-server'
        if conf_dir:
            return f'cd {shlex.quote(conf_dir)} && {inner}'
        return inner

    @staticmethod
    def _subscribe_mode_cli(mode: str) -> str:
        if mode in (GnmiMode.STREAM, '', 'stream'):
            return '--mode stream'
        if mode == GnmiMode.POLL:
            return '--mode poll'
        if mode == GnmiMode.ONCE:
            return '--mode once'
        return f'--mode {mode}'

    def _target_flag(self, target: Optional[str]) -> str:
        t = self.gnmi_target if target is None else target
        return f' --target {t}' if t else ''

    @staticmethod
    def _gnmic_full_cmd_is_subscribe(full_cmd: str) -> bool:
        return bool(re.search(r'\bsubscribe\b', full_cmd))

    @staticmethod
    def _default_first_update_hint(path: str) -> str:
        """Flat gnmic lines look like ``system/state/boot-time: <value>``."""
        p = path.strip('/')
        return f'{p}:' if p else ''

    def _remote_subscribe_log_path(self) -> str:
        return f'/tmp/{self.tunnel_name}_gnmic_subscribe.out'

    def _gnmic_remote_pkill_pattern(self) -> str:
        """Match our tunnel gnmic on the collector (config basename is unique per tunnel)."""
        return f'gnmic --config {os.path.basename(self.config_path)}'

    def _poll_remote_subscribe_log(
        self,
        log_path: str,
        substring: str,
        deadline: float,
        started_at: float,
    ) -> float:
        poll = 0.4
        while time.time() < deadline:
            hit = self._server_run(
                f'grep -F {shlex.quote(substring)} {shlex.quote(log_path)} 2>/dev/null | head -1 || true',
                timeout=min(self.cmd_time, 60),
            )
            if substring in hit:
                return time.time() - started_at
            time.sleep(poll)
        tail = self._server_run(f'tail -c 8000 {shlex.quote(log_path)} 2>/dev/null || true', timeout=60)
        raise AssertionError(
            f'no first update containing {substring!r} within timeout; tail:\n{tail}'
        )

    def _subscribe_keep_alive(
        self,
        gnmi_operation: str,
        *,
        wait_for_first_update_substring: str,
        first_update_timeout: int,
        debug: bool,
    ) -> Tuple[str, str, float, Optional[subprocess.Popen]]:
        self.stop_subscription()
        self.subscribe_first_update_seconds = None
        dbg = ' -d' if debug else ''
        cmd = f'{self._gnmic_base()}{dbg} {gnmi_operation}'.strip()
        started_at = time.time()
        with allure.step(
            f'subscribe keep-alive until first update {wait_for_first_update_substring[:48]!r}...'
        ):
            if self.server is not None:
                out_path = self._remote_subscribe_log_path()
                self._subscribe_bg_out = out_path
                start_script = (
                    f'OUT={shlex.quote(out_path)}; rm -f "$OUT"; : > "$OUT"; '
                    f'nohup bash -c {shlex.quote(cmd)} >> "$OUT" 2>&1 &'
                )
                self._server_run(start_script, timeout=90)
                deadline = started_at + first_update_timeout
                self.subscribe_first_update_seconds = self._poll_remote_subscribe_log(
                    out_path, wait_for_first_update_substring, deadline, started_at
                )
                snapshot = self._server_run(
                    f'tail -c 12000 {shlex.quote(out_path)} 2>/dev/null || true',
                    timeout=60,
                )
                wall = time.time() - started_at
                return snapshot, '', wall, None

            log_path = os.path.join(
                os.path.abspath(os.path.dirname(self.config_path) or '/tmp'),
                f'{self.tunnel_name}_gnmic_subscribe_local.out',
            )
            self._subscribe_local_log = log_path
            try:
                if os.path.isfile(log_path):
                    os.remove(log_path)
            except OSError:
                pass
            log_f = open(log_path, 'ab', buffering=0)
            try:
                proc = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    preexec_fn=os.setsid,
                )
                self._subscribe_local_proc = proc
            finally:
                log_f.close()
            deadline = started_at + first_update_timeout
            poll = 0.3
            while time.time() < deadline:
                try:
                    with open(log_path, 'r', errors='replace') as rf:
                        content = rf.read()
                except OSError:
                    content = ''
                if wait_for_first_update_substring in content:
                    self.subscribe_first_update_seconds = time.time() - started_at
                    wall = time.time() - started_at
                    return content, '', wall, proc
                if proc.poll() is not None:
                    raise AssertionError(
                        f'gnmic exited before first update; log tail:\n{content[-4000:]}'
                    )
                time.sleep(poll)
            with open(log_path, 'r', errors='replace') as rf:
                tail = rf.read()
            raise AssertionError(
                f'no first update containing {wait_for_first_update_substring!r}; tail:\n{tail[-4000:]}'
            )

    def run_gnmic(
        self,
        gnmi_operation: str,
        *,
        keep_session_alive: bool = False,
        wait_till_done: bool = True,
        cmd_timeout: Optional[int] = None,
        debug: bool = False,
    ) -> Tuple[str, str, float, Optional[subprocess.Popen]]:
        dbg = ' -d' if debug else ''
        cmd = f'{self._gnmic_base()}{dbg} {gnmi_operation}'.strip()
        with allure.step(f'gnmic tunnel: {gnmi_operation[:80]}'):
            if self.server is not None:
                if keep_session_alive:
                    raise NotImplementedError(
                        'use subscribe(..., keep_subscription_alive=True) for a background stream on the server'
                    )
                # Stream subscribe never returns; SSH engines wait for a shell prompt and time out.
                # Wrap with GNU timeout so gnmic is killed and the session gets a prompt again.
                if self._gnmic_full_cmd_is_subscribe(cmd):
                    run_sec = cmd_timeout if cmd_timeout is not None else self.DEFAULT_DIAL_OUT_VALIDATE_TIMEOUT
                    remote_cmd = f'timeout --foreground {int(run_sec)} bash -c {shlex.quote(cmd)}'
                    engine_timeout = int(run_sec) + 90
                else:
                    timeout = cmd_timeout
                    if wait_till_done:
                        timeout = timeout if timeout is not None else self.cmd_time
                    else:
                        timeout = timeout if timeout is not None else CmdRunner.DEFAULT_TIMEOUT
                    remote_cmd = cmd
                    engine_timeout = int(timeout) + 30
                start = time.time()
                out = self._server_run(remote_cmd, timeout=engine_timeout)
                duration = time.time() - start
                return out, '', duration, None
            start = time.time()
            out, err, proc = self.cmd_runner.run_cmd_in_process(
                cmd, keep_session_alive, wait_till_done, cmd_timeout
            )
            duration = time.time() - start
            return out, err, duration, proc

    def subscribe(
        self,
        path: str,
        mode: str = GnmiMode.STREAM,
        *,
        prefix: str = '',
        flat: bool = True,
        target: Optional[str] = None,
        keep_session_alive: bool = False,
        keep_subscription_alive: bool = False,
        wait_for_first_update_substring: Optional[str] = None,
        first_update_timeout: Optional[int] = None,
        wait_till_done: bool = True,
        cmd_timeout: Optional[int] = None,
        debug: bool = False,
    ) -> Tuple[str, str, float, Optional[subprocess.Popen]]:
        """
        With ``keep_subscription_alive=True``, the first telemetry line to wait for defaults from
        ``path``: leading slashes stripped and ``:`` appended (gnmic flat), e.g.
        ``/system/state/boot-time`` → ``system/state/boot-time:``. Override only if needed via
        ``wait_for_first_update_substring``.
        """
        allowed = GnmiMode.ALL_MODES if not keep_session_alive else [GnmiMode.STREAM, GnmiMode.POLL]
        assert mode in allowed, f'unsupported subscribe mode: {mode!r}'
        mode_cli = self._subscribe_mode_cli(mode)
        flat_arg = ' --format flat' if flat else ''
        prefix_arg = f" --prefix '{prefix}'" if prefix else ''
        op = (
            f'{mode_cli} subscribe{self._target_flag(target)}{prefix_arg} '
            f'--path {shlex.quote(path)}{flat_arg}'
        )
        if keep_subscription_alive:
            assert not keep_session_alive, 'use keep_subscription_alive without keep_session_alive'
            assert mode in (GnmiMode.STREAM, '', 'stream'), (
                'keep_subscription_alive is only supported for stream mode'
            )
            hint = wait_for_first_update_substring or self._default_first_update_hint(path)
            assert hint, 'use a non-empty subscribe path or set wait_for_first_update_substring='
            tout = first_update_timeout if first_update_timeout is not None else self.DEFAULT_DIAL_OUT_VALIDATE_TIMEOUT
            return self._subscribe_keep_alive(
                op.strip(),
                wait_for_first_update_substring=hint,
                first_update_timeout=int(tout),
                debug=debug,
            )
        return self.run_gnmic(op.strip(), keep_session_alive=keep_session_alive,
                              wait_till_done=wait_till_done, cmd_timeout=cmd_timeout, debug=debug)

    def verify_tunnel_subscribe_success(
        self,
        stdout: str,
        stderr: str,
        *,
        expected_target_id: str = 'nvos',
        subscribe_path: str = '/system',
        require_subscribe_request: bool = True,
        require_telemetry_line: bool = True,
        telemetry_substring: Optional[str] = None,
    ) -> None:
        """
        Assert gnmic dial-out tunnel subscribe reached a healthy state (logs + optional telemetry).

        gnmic prints progress to stderr; telemetry (e.g. flat lines) typically goes to stdout.
        Expected sequence matches successful runs such as:
        discovered target -> subscribe -> client created -> tunnel dial -> SubscribeRequest -> updates.
        """
        with allure.step('verify tunnel subscribe output (dial-out success markers)'):
            combined = f'{stdout}\n{stderr}'
            tail = combined[-4000:] if len(combined) > 4000 else combined

            assert self._LOG_TUNNEL_DISCOVERED in combined, (
                f'missing tunnel target discovery ({self._LOG_TUNNEL_DISCOVERED!r}).\n--- tail ---\n{tail}'
            )
            id_marker = f'ID:{expected_target_id}'
            assert id_marker in combined, (
                f'missing discovered target id {id_marker!r}.\n--- tail ---\n{tail}'
            )
            assert self._LOG_SUBSCRIBING in combined, (
                f'missing subscribe start ({self._LOG_SUBSCRIBING!r}).\n--- tail ---\n{tail}'
            )
            assert f'"{expected_target_id}"' in combined, (
                f'missing target name {expected_target_id!r} in log output.\n--- tail ---\n{tail}'
            )
            assert self._LOG_GNMI_CLIENT in combined, (
                f'missing gNMI client ({self._LOG_GNMI_CLIENT!r}).\n--- tail ---\n{tail}'
            )
            assert self._LOG_DIALING_TUNNEL in combined, (
                f'missing tunnel dial ({self._LOG_DIALING_TUNNEL!r}).\n--- tail ---\n{tail}'
            )
            if require_subscribe_request:
                assert self._LOG_SUBSCRIBE_REQUEST in combined, (
                    f'missing SubscribeRequest log ({self._LOG_SUBSCRIBE_REQUEST!r}).\n--- tail ---\n{tail}'
                )
                root = subscribe_path.strip('/').split('/')[0]
                if root:
                    assert f'name:"{root}"' in combined, (
                        f'expected path root {root!r} in SubscribeRequest log line.\n--- tail ---\n{tail}'
                    )
            if require_telemetry_line:
                hint = telemetry_substring
                if hint is None:
                    root = subscribe_path.strip('/').split('/')[0]
                    hint = f'{root}/' if root else None
                if hint:
                    stream_out = stdout or stderr
                    assert hint in stream_out, (
                        f'expected flat telemetry containing {hint!r} in command output '
                        f'(subscribe path {subscribe_path!r}).\n--- tail ---\n'
                        f'{stream_out[-2000:]!s}'
                    )

    def validate_dial_out_connection(
        self,
        path: str = '/system',
        *,
        target: Optional[str] = None,
        mode: str = GnmiMode.STREAM,
        flat: bool = True,
        cmd_timeout: Optional[int] = None,
        debug: bool = True,
        **verify_kw: Any,
    ) -> Tuple[str, str, float]:
        """
        Run a stream subscribe with --use-tunnel-server, capture output for cmd_timeout, then verify.

        The stream is stopped when cmd_timeout elapses (CmdRunner kills the process); output gathered to
        that point must contain the success markers and, by default, at least one flat telemetry line.

        To assert latency from gnmic start until the first flat line, use
        :meth:`subscribe` with ``keep_subscription_alive=True`` and read
        :attr:`subscribe_first_update_seconds`.
        """
        with allure.step(f'validate dial-out tunnel subscribe path={path!r}'):
            timeout = cmd_timeout if cmd_timeout is not None else self.DEFAULT_DIAL_OUT_VALIDATE_TIMEOUT
            out, err, duration, _ = self.subscribe(
                path,
                mode=mode,
                flat=flat,
                target=target,
                keep_session_alive=False,
                wait_till_done=False,
                cmd_timeout=timeout,
                debug=debug,
            )
            self.verify_tunnel_subscribe_success(
                out,
                err,
                subscribe_path=path,
                **verify_kw,
            )
            return out, err, duration

    def capabilities(
        self,
        *,
        target: Optional[str] = None,
        wait_till_done: bool = True,
        cmd_timeout: Optional[int] = None,
        debug: bool = False,
    ) -> Tuple[str, str, float, Optional[subprocess.Popen]]:
        op = f'capabilities{self._target_flag(target)}'.strip()
        return self.run_gnmic(op, wait_till_done=wait_till_done, cmd_timeout=cmd_timeout, debug=debug)

    def get_path(
        self,
        path: str,
        *,
        target: Optional[str] = None,
        wait_till_done: bool = True,
        cmd_timeout: Optional[int] = None,
        debug: bool = False,
    ) -> Tuple[str, str, float, Optional[subprocess.Popen]]:
        op = f'get{self._target_flag(target)} --path {shlex.quote(path)}'.strip()
        return self.run_gnmic(op, wait_till_done=wait_till_done, cmd_timeout=cmd_timeout, debug=debug)

    def close_session_and_get_out_and_err(self, process: subprocess.Popen, delay=0) -> Tuple[str, str]:
        if self.server is not None:
            raise NotImplementedError('remote server mode does not expose a local Popen; use local server=None')
        return self.cmd_runner.kill_cmd_process(process, delay)

    def stop_subscription(self) -> None:
        """
        Stop a stream started with ``subscribe(..., keep_subscription_alive=True)``.

        A second ``run_cmd`` cannot send Ctrl+C to gnmic started in an earlier SSH invocation
        (new shell each time). We send the same signal as Ctrl+C with ``pkill -INT -f`` on a
        command-line pattern that includes this instance's config file name.
        """
        with allure.step('stop background gnmic subscribe'):
            if self.server is not None:
                pat = self._gnmic_remote_pkill_pattern()
                quoted = shlex.quote(pat)
                self._server_run(
                    f'pkill -INT -f {quoted} 2>/dev/null || true; '
                    f'sleep 2; pkill -TERM -f {quoted} 2>/dev/null || true; '
                    f'sleep 1; pkill -9 -f {quoted} 2>/dev/null || true',
                    timeout=90,
                )
                self._subscribe_bg_out = None
            if self._subscribe_local_proc is not None:
                try:
                    self.cmd_runner.kill_cmd_process(self._subscribe_local_proc, delay=0)
                except Exception:
                    try:
                        os.killpg(os.getpgid(self._subscribe_local_proc.pid), signal.SIGTERM)
                    except (ProcessLookupError, OSError):
                        pass
                self._subscribe_local_proc = None
            if self._subscribe_local_log and os.path.isfile(self._subscribe_local_log):
                try:
                    os.remove(self._subscribe_local_log)
                except OSError:
                    pass
                self._subscribe_local_log = None

    def stop_subscribtion(self) -> None:
        """Alias for :meth:`stop_subscription` (common typo)."""
        self.stop_subscription()

    def _subscribe_local_log_path(self) -> str:
        conf_dir = os.path.dirname(self.config_path) or '/tmp'
        return os.path.join(
            os.path.abspath(conf_dir),
            f'{self.tunnel_name}_gnmic_subscribe_local.out',
        )

    def delete(self) -> None:
        """
        Remove gnmic tunnel-server artifacts (config, TLS material, subscribe logs).

        Default remote layout matches ``/tmp/<tunnel_name>_dial_out_grpc.yaml``,
        ``_gnmic-cert.pem``, ``_gnmic-key.pem``, and ``_gnmic_subscribe.out``.
        """
        paths = [
            self.config_path,
            self.tls_cert_file,
            self.tls_key_file,
            self._remote_subscribe_log_path(),
            self._subscribe_local_log_path(),
        ]
        if self.tls_ca_file:
            paths.append(self.tls_ca_file)
        seen: set[str] = set()
        unique: list[str] = []
        for p in paths:
            if p and p not in seen:
                seen.add(p)
                unique.append(p)

        with allure.step(f'remove tunnel collector files for {self.tunnel_name!r}'):
            if self.server is not None:
                for p in unique:
                    self._server_run(f'rm -f {shlex.quote(p)} 2>/dev/null || true', timeout=60)
            else:
                for p in unique:
                    try:
                        if os.path.isfile(p):
                            os.remove(p)
                    except OSError:
                        pass

    def __enter__(self) -> 'GrpcTunnelServer':
        self.write_config()
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def _log(self, msg: str):
        logging.info(f'[GrpcTunnelServer] {msg}')
