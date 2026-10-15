import ipaddress
import logging
import os
import re
import signal
import subprocess
import tempfile
import time
import xmlrpc.client
from typing import Iterable, Tuple, List

REDACTED = '<redacted>'


def redact(text: str, secrets: Iterable[str]) -> str:
    """
    Replace each given secret with a placeholder, for a command about to be logged.

    Matching is on the exact secret rather than on a flag name, because '-p' means something else
    in most commands that carry it ('mkdir -p') and masking its argument would make those log
    lines useless.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


class CmdRunner:
    """
    This class serves as a tool to run given commands directly on the running player,
        using subprocess/Popen module

    spool_output writes command output to temp files instead of pipes, for commands whose output is
    read only once they are closed: an undrained pipe buffer fills up and blocks the command. It
    leaves process.stdout/stderr as None, so callers that read the process directly cannot use it.
    """
    DEFAULT_TIMEOUT = 30
    MAX_LOGGED_OUTPUT = 100_000
    # How long a process gets to exit on SIGTERM before it is killed outright.
    TERMINATE_GRACE_SEC = 10

    def __init__(self, runner_name: str = '', default_timeout=DEFAULT_TIMEOUT, print_outputs: bool = True,
                 kill_live_processes_on_delete: bool = True, spool_output: bool = False):
        self._runner_name = runner_name
        self._default_timeout = default_timeout
        self._live_processes: List[subprocess.Popen] = []
        self._own_processes: dict = {}  # process -> its spool files, or None when it uses pipes
        self._kill_live_processes_on_delete = kill_live_processes_on_delete
        self._print_outputs_to_log = print_outputs
        self.spool_output = spool_output

    def __del__(self):
        if self._kill_live_processes_on_delete:
            self._log('close live processes')
            for process in list(self._live_processes):
                self.kill_cmd_process(process, kill_only=True)

    def run_cmd(self, cmd: str, allowed_err: str = '') -> str:
        """
        run a given command
            - wait till command is done
            - asserts that there's no error
        @param cmd: the command to run
        @param allowed_err: regex pattern to allow in err channel of the running command.
            if specified, and if regex pattern matches the err channel (exists in), then ignore the error.
        @return: the output of the command
        """
        out, err, _ = self.run_cmd_in_process(cmd)

        self._log(f'verify command had no errors in err channel')
        cmd_ok = not err
        if allowed_err:
            cmd_ok = cmd_ok or bool(re.search(allowed_err, err))
        assert cmd_ok, f'command failed with error in err channel.\ncmd: "{cmd}"\nerr:\n{err}'

        return out.strip()

    def run_cmd_in_process(self, cmd: str, keep_process_alive: bool = False, wait_till_done: bool = True,
                           cmd_timeout=None, secrets: Iterable[str] = ()) -> Tuple[str, str, subprocess.Popen]:
        """
        run a given command in a process
        @param cmd: the command to run
        @param keep_process_alive: whether to keep the process alive (and return it) or not
        @param wait_till_done: if not keep_process_alive - whether to wait till the command is finished or not
        @param cmd_timeout: number of seconds to give to the command, after which its process is
            killed and whatever it produced so far is returned. Honored whether or not
            wait_till_done is set: a command given a deadline is bounded by it either way.
            When it is not provided, wait_till_done waits indefinitely, while a caller that did
            not want to block gets the default_timeout initialized with the object.
        @param secrets: credentials embedded in cmd, masked in the logged copy of it.
            The command itself runs unchanged.
        @return: several options:
            - when keep_process_alive is True - return the running process
                * the process is added to internal list of the opened processes
                * eventually when (before) self object is deleted, all saved open processes are killed
            - otherwise - return output, err of the command process
        """
        self._log(f"run: {redact(cmd, secrets)}")
        spools = (tempfile.TemporaryFile(), tempfile.TemporaryFile()) if self.spool_output else None
        stdout, stderr = spools or (subprocess.PIPE, subprocess.PIPE)
        process = subprocess.Popen(cmd, shell=True, stdout=stdout, stderr=stderr, process_group=0)
        self._own_processes[process] = spools

        if keep_process_alive:
            self._log(f"keeping process alive and returning it")
            self._live_processes.append(process)
            return '', '', process

        if wait_till_done and cmd_timeout is None:
            self._log(f"wait till cmd is done and get output")
            out, err = self.wait_for_cmd_process(process)
            return out, err, None

        # Either the caller does not want to block, or it gave the command a deadline. A deadline
        # has to be enforced even when waiting for the command to finish, or a hung command (a
        # stalled transfer, a PDU that never answers) blocks the run with nothing to time it out.
        output, err = self.kill_cmd_process(process, cmd_timeout or self._default_timeout)
        return output, err, None

    def wait_cmd_process(self, process: subprocess.Popen, timeout=None) -> Tuple[str, str]:
        """Wait up to timeout for a kept-alive process to finish on its own, then collect output."""
        return self.kill_cmd_process(process, delay=timeout if timeout is not None else self._default_timeout)

    def kill_cmd_process(self, process: subprocess.Popen, delay=0, kill_only: bool = False) -> Tuple[str, str]:
        """
        kill a given process, after a given delay
        @param process: given process to kill
        @param delay: number of seconds to wait before killing the process
        @return: output, err of the command process
        @param kill_only: if True, just kill the process, and don't get its output
        """
        if kill_only:
            self._terminate(process, signal.SIGKILL)
            process.wait()
            self._forget(process)
            return '', ''

        self._log(f'wait for process done or kill it by {delay} seconds')
        try:
            process.wait(timeout=delay)
            self._log('process finished normally')
        except subprocess.TimeoutExpired:
            self._log(f'{delay} seconds passed - kill the process')
            self._terminate(process, signal.SIGTERM)
            try:
                process.wait(timeout=self.TERMINATE_GRACE_SEC)
            except subprocess.TimeoutExpired:
                # Collecting the output below waits on the process with no timeout of its own, so
                # one that sits on SIGTERM would make the deadline we just enforced meaningless.
                self._log('the process is still alive after SIGTERM - sending SIGKILL')
                self._terminate(process, signal.SIGKILL)
        return self.wait_for_cmd_process(process)

    def wait_for_cmd_process(self, process: subprocess.Popen) -> Tuple[str, str]:
        """
        wait for a given process to finish
        @param process: given process to wait for
        @return: output, err of the command process
        """
        spools = self._own_processes.get(process)
        if spools is None:  # communicate() drains the pipes; waiting first deadlocks on a full buffer
            output, err = (stream.decode('utf-8') for stream in process.communicate())
        else:
            process.wait()
            output, err = (self._read_spool(spool) for spool in spools)
        self._forget(process)
        if self._print_outputs_to_log:
            self._log(f"output: {self._shorten_for_log(output)}")
            self._log(f"err: {self._shorten_for_log(err)}")
        return output, err

    def _terminate(self, process: subprocess.Popen, sig: int):
        """Signal our own processes by group so descendants die too; a foreign one shares our group."""
        if process.poll() is not None:
            return
        if process not in self._own_processes:
            process.kill()
            return
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except ProcessLookupError:
            pass  # exited between the poll and the signal

    def _forget(self, process: subprocess.Popen):
        """Drop a finished process, so it is not killed again once its pid can be reused."""
        for spool in self._own_processes.pop(process, None) or ():
            spool.close()
        if process in self._live_processes:
            self._live_processes.remove(process)

    @staticmethod
    def _read_spool(spool) -> str:
        with spool:
            spool.seek(0)
            return spool.read().decode('utf-8', errors='replace')

    @staticmethod
    def _shorten_for_log(text: str) -> str:
        """Shorten the logged copy only; the caller still gets the whole output."""
        if len(text) <= CmdRunner.MAX_LOGGED_OUTPUT:
            return text
        return f'{text[:CmdRunner.MAX_LOGGED_OUTPUT]}\n... truncated for the log ({len(text)} chars total)'

    def _log(self, message: str):
        logging.info(f"{f'[{self._runner_name}] ' if self._runner_name else ''}{message}")


class _TimeoutTransport(xmlrpc.client.Transport):
    """XML-RPC transport with a client-side socket timeout, so a stalled connection can't hang us."""

    def __init__(self, timeout=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        if self.timeout is not None:
            conn.timeout = self.timeout
            if getattr(conn, 'sock', None) is not None:  # update an already-open reused socket too
                conn.sock.settimeout(self.timeout)
        return conn


class EngineProcessHandle:
    """A process running on a remote engine. Plays the role of subprocess.Popen for EngineCmdRunner."""

    def __init__(self, proxy, p_uuid: str, cmd: str, player=None):
        self.proxy = proxy
        self.p_uuid = p_uuid
        self.cmd = cmd
        self.player = player
        self.output = None
        self.err = None
        self.rc = None
        self.closed = False


class EngineCmdRunner:
    """
    Like CmdRunner, but runs commands on a remote engine over its XML-RPC process server
    instead of as local processes.

    Implements the same methods GnmiClient uses (run_cmd / run_cmd_in_process /
    wait_cmd_process / kill_cmd_process), so callers can run tools like gnmic on a remote
    engine without knowing the difference. stdout and stderr are kept separate, so callers
    that check the err channel still work.

    Each instance keeps its own ServerProxy so parallel runners don't share one socket.
    """
    DEFAULT_TIMEOUT = 30
    # Extra time added to the server-side wait to get the socket timeout, so the socket only
    # fires on a truly stuck connection, not on a normal long wait.
    RPC_SOCKET_TIMEOUT_BUFFER_SEC = 30
    COLLECT_MAX_ATTEMPTS = 3
    COLLECT_RETRY_DELAY_SEC = 2

    def __init__(self, engine, runner_name: str = '', default_timeout=DEFAULT_TIMEOUT,
                 print_outputs: bool = True, kill_live_processes_on_delete: bool = True):
        self._engine = engine
        self._runner_name = runner_name
        self._default_timeout = default_timeout
        self._print_outputs_to_log = print_outputs
        self._kill_live_processes_on_delete = kill_live_processes_on_delete
        self.spool_output = False  # no local pipe to fill: the remote process server holds the output
        self._live_handles: List[EngineProcessHandle] = []
        _ = engine.process_proxy  # makes sure the remote process server is up
        xml_rpc_port = getattr(engine, 'xml_rpc_port', 9999)
        self._transport = _TimeoutTransport()
        host = self._format_host_for_url(engine.ip)
        self._proxy = xmlrpc.client.ServerProxy(f'http://{host}:{xml_rpc_port}/',
                                                transport=self._transport)

    @staticmethod
    def _format_host_for_url(host: str) -> str:
        """Wrap an IPv6 address in brackets so the URL stays valid: http://[::1]:port/."""
        try:
            if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
                return f'[{host}]'
        except ValueError:
            pass
        return host

    def __del__(self):
        if self._kill_live_processes_on_delete:
            self._log('close live processes')
            for handle in list(self._live_handles):
                try:
                    self.kill_cmd_process(handle, kill_only=True)
                except Exception:
                    pass

    def run_cmd(self, cmd: str, allowed_err: str = '') -> str:
        """Run a command, fail if anything lands on the err channel (unless allowed_err matches)."""
        out, err, _ = self.run_cmd_in_process(cmd)
        cmd_ok = not err
        if allowed_err:
            cmd_ok = cmd_ok or bool(re.search(allowed_err, err))
        assert cmd_ok, f'command failed with error in err channel.\ncmd: "{cmd}"\nerr:\n{err}'
        return out.strip()

    def run_cmd_in_process(self, cmd: str, keep_process_alive: bool = False, wait_till_done: bool = True,
                           cmd_timeout=None, secrets: Iterable[str] = ()) -> Tuple[str, str, EngineProcessHandle]:
        """
        Start a command on the remote engine. Run via 'bash -c' so quoting works like a shell.

        secrets are credentials embedded in cmd, masked in the logged copy of it.
        """
        self._log(f"run on {self._engine.ip}: {redact(cmd, secrets)}")
        p_uuid = self._proxy.run_process(['bash', '-c', cmd])
        handle = EngineProcessHandle(self._proxy, p_uuid, cmd, player=self._engine)

        if keep_process_alive:
            self._log("keeping process alive and returning it")
            self._live_handles.append(handle)
            return '', '', handle

        # Wait for it to finish; the remote server kills it if it runs past the timeout.
        timeout = cmd_timeout or self._default_timeout
        out, err = self._collect(handle, 'wait_process', timeout)
        return out, err, None

    def wait_cmd_process(self, process: EngineProcessHandle, timeout=None) -> Tuple[str, str]:
        """Wait for a kept-alive process to finish on its own, then grab its output."""
        if process is None or getattr(process, 'closed', False):
            return '', ''
        return self._collect(process, 'wait_process', timeout or self._default_timeout)

    def kill_cmd_process(self, process: EngineProcessHandle, delay=0, kill_only: bool = False) -> Tuple[str, str]:
        """Stop a remote process (optionally after a delay) and collect its output."""
        if process is None or getattr(process, 'closed', False):
            return '', ''
        if delay:
            self._log(f'wait {delay} seconds before stopping remote process')
            time.sleep(delay)
        out, err = self._collect(process, 'stop_and_wait_process', self._default_timeout)
        if kill_only:
            return '', ''
        return out, err

    def _collect(self, handle: EngineProcessHandle, action: str, timeout) -> Tuple[str, str]:
        """
        Finish a remote process and read its output, where action is 'wait_process' or
        'stop_and_wait_process'.

        We retry the call a few times, and only mark the handle done once it actually succeeds,
        so a flaky network never leaves a process running behind our back. If waiting keeps
        failing we try to stop it instead, and if that fails too we raise.
        """
        # Give the socket a bit more time than the wait itself, or a normal long wait would trip it.
        self._transport.timeout = (timeout or 0) + self.RPC_SOCKET_TIMEOUT_BUFFER_SEC

        last_exc = None
        for attempt in range(1, self.COLLECT_MAX_ATTEMPTS + 1):
            try:
                rc, out, err = getattr(handle.proxy, action)(handle.p_uuid, timeout)
                return self._store_collected(handle, rc, out, err)
            except Exception as err_obj:
                last_exc = err_obj
                self._log(f'attempt {attempt}/{self.COLLECT_MAX_ATTEMPTS} to {action} remote '
                          f'process {handle.p_uuid} failed: {err_obj}')
                if attempt < self.COLLECT_MAX_ATTEMPTS:
                    time.sleep(self.COLLECT_RETRY_DELAY_SEC)

        # Couldn't just wait, so try to stop it instead of leaking it.
        if action == 'wait_process':
            self._log(f'falling back to stop_and_wait_process for remote process {handle.p_uuid}')
            try:
                rc, out, err = handle.proxy.stop_and_wait_process(handle.p_uuid, timeout)
                return self._store_collected(handle, rc, out, err)
            except Exception as stop_exc:
                self._log(f'fallback stop of remote process {handle.p_uuid} also failed: {stop_exc}')

        # Keep the handle tracked so __del__ can try once more.
        raise RuntimeError(
            f'failed to {action} remote process {handle.p_uuid} after {self.COLLECT_MAX_ATTEMPTS} '
            f'attempts') from last_exc

    def _store_collected(self, handle: EngineProcessHandle, rc, out, err) -> Tuple[str, str]:
        """Save the result on the handle and stop tracking it as live."""
        handle.rc, handle.output, handle.err = rc, out, err
        handle.closed = True
        if handle in self._live_handles:
            self._live_handles.remove(handle)
        if self._print_outputs_to_log:
            self._log(f"output: {out}")
            self._log(f"err: {err}")
        return out, err

    def _log(self, message: str):
        logging.info(f"{f'[{self._runner_name}] ' if self._runner_name else ''}{message}")
