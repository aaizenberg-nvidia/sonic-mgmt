"""
gnoic client wrapper, mirroring GnmiClient so gNOI tests drive the same runner infrastructure.

Only the RPCs the current tests need are wrapped; run_gnoic_op() is public so a new RPC can be
driven without extending this class first.
"""
import logging
import shlex
from typing import Tuple

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_tools.infra.CmdRunner import CmdRunner, EngineCmdRunner
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.tests_nvos.system.gnoi.constants import (
    GNOI_DEFAULT_PORT,
    GNOIC_CMD_TIMEOUT_SEC,
    GnoicErr,
)


class GnoiClient:
    def __init__(self, server_host, server_port=GNOI_DEFAULT_PORT, username='', password='',
                 cmd_time: int = GNOIC_CMD_TIMEOUT_SEC, cacert='', verify_tools_installed: bool = False,
                 print_outputs: bool = True, engine=None):
        assert cmd_time >= 0, f'unsupported cmd time: {cmd_time}. must be >= 0'

        self.server_host = server_host
        self.server_port = server_port
        self.username = username
        self.password = password
        self.cacert = cacert
        self.cmd_time = cmd_time

        if engine is None:
            # Spooled rather than piped: a multi-GB transfer logs a progress line per chunk, which
            # overruns the 64KB pipe buffer. A backgrounded op then blocks writing to it and the
            # transfer stalls with no way to tell that apart from the switch hanging.
            self.cmd_runner = CmdRunner('GnoiClient', self.cmd_time, print_outputs,
                                        spool_output=True)
        else:
            self.cmd_runner = EngineCmdRunner(engine, 'GnoiClient', self.cmd_time, print_outputs)

        if verify_tools_installed:
            with allure.step('verify gnoic installed on player'):
                self.verify_gnoic_installation()

    def verify_gnoic_installation(self):
        """
        Assert gnoic is available. Unlike gnmic there is no agreed install one-liner for the lab
        (OPEN-12), so a missing binary is a setup failure rather than something to auto-install.
        """
        out, err, _ = self.cmd_runner.run_cmd_in_process('gnoic version', cmd_timeout=self.cmd_time)
        installed = GnoicErr.NOT_INSTALLED not in out and GnoicErr.NOT_INSTALLED not in err \
            and 'not found' not in out and 'not found' not in err
        assert installed, ('gnoic is not installed where the test runs. Install gnoic 0.2.x on the '
                           f'test player / engine and retry.\nout: {out}\nerr: {err}')
        self._log(f'gnoic version: {out.strip() or err.strip()}')

    def _password_to_use(self, password: str = '') -> str:
        """The password a call authenticates with: its own, or the client's by default."""
        return password or self.password

    def compose_gnoic_cmd(self, gnoic_op: str, skip_cert_verify: bool = True, cacert: str = '',
                          username: str = '', password: str = '') -> str:
        """
        Build the gnoic command without running it.

        gnoic takes the endpoint as '-a host:port' (one flag), unlike gnmic's '-a host --port'.

        The password is an argument because gnoic has no other way to take it, so a caller running
        this command itself must keep it out of whatever it logs - run_gnoic_op does.
        """
        username = username or self.username
        password = self._password_to_use(password)

        if skip_cert_verify:
            cert_flag = '--skip-verify'
        else:
            cacert_to_use = cacert or self.cacert
            assert cacert_to_use, 'cacert path was not specified'
            cert_flag = f'--tls-ca {shlex.quote(cacert_to_use)}'

        host = f'[{self.server_host}]' if IpTool.is_address_ipv6(self.server_host) else self.server_host
        address = f'{host}:{self.server_port}'

        # gnoic_op carries its own quoting (e.g. --path '/etc/os-release'), so it is not quoted here.
        return (f"gnoic -a {shlex.quote(address)} -u {shlex.quote(username)} "
                f"-p {shlex.quote(password)} {cert_flag} {gnoic_op}")

    def run_gnoic_op(self, gnoic_op: str, skip_cert_verify: bool = True, cacert: str = '',
                     username: str = '', password: str = '', cmd_time=None) -> Tuple[str, str]:
        with allure.step(f'run gnoic {gnoic_op}'):
            cmd = self.compose_gnoic_cmd(gnoic_op, skip_cert_verify, cacert, username, password)
            out, err, _ = self.cmd_runner.run_cmd_in_process(cmd, cmd_timeout=cmd_time or self.cmd_time,
                                                             secrets=[self._password_to_use(password)])
            return out, err

    def start_gnoic_op(self, gnoic_op: str, skip_cert_verify: bool = True, cacert: str = '',
                       username: str = '', password: str = ''):
        """
        Launch a gnoic op and return its process without waiting.

        Needed for anything that has to be observed while it is still running - a transfer's staged
        file, or a second RPC competing with it for the install lock - which a blocking call cannot
        express. Pair with wait_gnoic_op, and note that a client of its own is advisable for the
        concurrent op: two calls sharing one runner also share its process list.
        """
        cmd = self.compose_gnoic_cmd(gnoic_op, skip_cert_verify, cacert, username, password)
        _, _, process = self.cmd_runner.run_cmd_in_process(cmd, keep_process_alive=True,
                                                           secrets=[self._password_to_use(password)])
        return process

    def wait_gnoic_op(self, process, cmd_time=None) -> Tuple[str, str]:
        """Wait for a backgrounded op to finish and collect its output."""
        return self.cmd_runner.wait_cmd_process(process, timeout=cmd_time or self.cmd_time)

    def file_stat(self, path: str, **kwargs) -> Tuple[str, str]:
        """File.Stat - metadata only, the least intrusive way to prove gNOI answers."""
        return self.run_gnoic_op(f"file stat --path {shlex.quote(path)}", **kwargs)

    def file_put(self, local_file: str, dst: str, permission: int = None, **kwargs) -> Tuple[str, str]:
        """
        File.Put - stream a local file onto the switch.

        permission is octal; gnoic defaults to 777 and treats 0 as "keep the local file's mode".
        The destination must be writable by the user the RPC authenticates as, since the server
        chowns what it writes to that user.
        """
        op = f"file put --file {shlex.quote(local_file)} --dst {shlex.quote(dst)}"
        if permission is not None:
            op += f" --permission {permission}"
        return self.run_gnoic_op(op, **kwargs)

    def start_file_put(self, local_file: str, dst: str, permission: int = None, **kwargs):
        """File.Put without waiting - for tests that inspect the switch mid-transfer."""
        op = f"file put --file {shlex.quote(local_file)} --dst {shlex.quote(dst)}"
        if permission is not None:
            op += f" --permission {permission}"
        return self.start_gnoic_op(op, **kwargs)

    def start_os_install(self, pkg: str, version: str, print_proto: bool = True, **kwargs):
        """OS.Install without waiting - for tests that compete with an install in flight."""
        op = f"os install --pkg {shlex.quote(pkg)} --version {shlex.quote(version)}"
        if print_proto:
            op = f"--print-proto {op}"
        return self.start_gnoic_op(op, **kwargs)

    def system_set_package(self, pkg: str, dst: str, version: str, activate: bool = False,
                           **kwargs) -> Tuple[str, str]:
        """
        System.SetPackage - platform firmware install.

        On NVOS `version` carries the target component ('full' for a whole bundle, otherwise a
        component name) and `dst` the filename to store it under. Without activate the server only
        fetches; with it, the fetch is followed by an install, which writes firmware.
        """
        op = (f"system set-package --pkg {shlex.quote(pkg)} --dst {shlex.quote(dst)} "
              f"--version {shlex.quote(version)}")
        if activate:
            op += ' --activate'
        return self.run_gnoic_op(op, **kwargs)

    def file_get(self, remote_file: str, local_dir: str, **kwargs) -> Tuple[str, str]:
        """File.Get - pull a file off the switch into local_dir, which must already exist."""
        return self.run_gnoic_op(
            f"file get --file {shlex.quote(remote_file)} --dst {shlex.quote(local_dir)}", **kwargs)

    def file_remove(self, path: str, **kwargs) -> Tuple[str, str]:
        """File.Remove - delete a file on the switch, as the authenticated user."""
        return self.run_gnoic_op(f"file remove --path {shlex.quote(path)}", **kwargs)

    def os_install(self, pkg: str, version: str, chunk_size: int = None, print_proto: bool = True,
                   **kwargs) -> Tuple[str, str]:
        """
        OS.Install - stream a NOS image onto the switch.

        The switch stores the image under a name built from --version, not from the local filename,
        so the two are deliberately independent. Transferring ~3GB takes minutes; pass a cmd_time
        well above the default or the client gives up mid-transfer.

        print_proto is on by default because gnoic's log lines cover the transfer only: the
        TransferReady and Validated responses that say whether the install was accepted appear
        nowhere in its text output.
        """
        op = f"os install --pkg {shlex.quote(pkg)} --version {shlex.quote(version)}"
        if print_proto:
            op = f"--print-proto {op}"
        if chunk_size is not None:
            op += f" --content-chunk-size {chunk_size}"
        return self.run_gnoic_op(op, **kwargs)

    def os_activate(self, version: str, no_reboot: bool = True, **kwargs) -> Tuple[str, str]:
        """
        OS.Activate - select an already-transferred image for the next boot.

        no_reboot keeps activation and rebooting separable, so a test can observe the boot-next
        change before deciding to reboot.
        """
        op = f"os activate --version {shlex.quote(version)}"
        if no_reboot:
            op += " --no-reboot"
        return self.run_gnoic_op(op, **kwargs)

    def os_verify(self, **kwargs) -> Tuple[str, str]:
        """OS.Verify - report the running version. Not in the supported eight; expected Unimplemented."""
        return self.run_gnoic_op("os verify", **kwargs)

    def system_reboot(self, method: str, **kwargs) -> Tuple[str, str]:
        """
        System.Reboot. COLD/HALT actually reboot or halt the switch; WARM is relayed as an NVUE
        rejection and the remaining modes fail with InvalidArgument without touching the switch.
        """
        return self.run_gnoic_op(f"system reboot --method {method}", **kwargs)

    def _log(self, msg: str):
        logging.info(f"[GnoiClient] {msg}")
