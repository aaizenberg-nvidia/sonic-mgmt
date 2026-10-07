"""Pin the unattended serial command shared by the serial logger and track_serial_console: every ssh rcon (ACS or
lab console host) gets a real TERM, the VM_PASSWORD login (read from the environment at run time, never inlined) and
one forced tty; telnet passes through. NOGA strings carry ``-l``/``-y`` so this holds with older devts tags too."""
import pytest

from ngts.tools.serial_logger import serial_log_script


@pytest.fixture
def noga_serial_cmd(monkeypatch):
    monkeypatch.setenv('VM_PASSWORD', 'secret-pw')

    def set_cmd(cmd):
        monkeypatch.setattr(serial_log_script, 'get_serial_connection_command', lambda _ip: cmd)
    return set_cmd


def _connect_part(logger_cmd):
    return logger_cmd.split('while :; do ', 1)[1].split('; sleep 10; done', 1)[0]


@pytest.mark.parametrize('noga_cmd, expected_tail', [
    ('ssh -l rcon:7007 10.7.150.239', '-l rcon:7007 10.7.150.239'),
    ('ssh -l root -t sws-console-1 console -y mag-145', '-l root sws-console-1 console -y mag-145'),
])
def test_ssh_rcon_logger_command(noga_serial_cmd, noga_cmd, expected_tail):
    noga_serial_cmd(noga_cmd)
    connect = _connect_part(serial_log_script.get_serial_logger_command('10.0.0.1', 'dut'))
    assert connect.startswith('TERM=vt100 SSHPASS="$VM_PASSWORD" sshpass -e ssh -tt ')
    assert connect.endswith(expected_tail)
    # a third -t (NOGA's -t after -tt) turns forced tty allocation back off: no remote pty without a local tty
    assert connect.split().count('-t') == 0
    assert 'secret-pw' not in connect


def test_telnet_logger_command_unchanged(noga_serial_cmd):
    noga_serial_cmd('telnet fit-vrt-60 10081')
    assert _connect_part(serial_log_script.get_serial_logger_command('10.0.0.1', 'dut')) == 'telnet fit-vrt-60 10081'
