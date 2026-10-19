import time
from contextlib import contextmanager

from infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine

from ngts.nvos_tools.infra.TcpdumpCmdBuilder import TcpdumpCmdBuilder


@contextmanager
def serial_capture_interface_traffic(serial_engine: PexpectSerialEngine, dut_engine: LinuxSshEngine, tcpdump_cmd: TcpdumpCmdBuilder, local_file_path: str):
    ts = int(time.time())
    remote_tmpfile = f"/tmp/capture_{ts}.pcap"
    tcpdump_cmd = tcpdump_cmd.write_file(remote_tmpfile)
    tcpdump_cmd_str = tcpdump_cmd.build()
    serial_engine.serial_engine.sendline(tcpdump_cmd_str)
    serial_engine.serial_engine.expect(rb"listening on .+", timeout=1)
    try:
        yield
    finally:
        serial_engine.serial_engine.sendline(b'\x03')
        dut_engine.copy_file(
            source_file=remote_tmpfile,
            dest_file=local_file_path,
            file_system='/',
            direction='get',
            overwrite_file=True,
            verify_file=True,
        )
        dut_engine.run_cmd(f"rm -f {remote_tmpfile}")
