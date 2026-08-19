"""Shared helpers for UDS Network Isolation tests."""
import logging
import re
import shlex
from dataclasses import dataclass
from typing import List, Set, Tuple

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

# ``check_can_write`` markers: must not be substrings of each other (unlike
# WRITABLE / NOT_WRITABLE). Compared against the last non-empty line of SSH
# stdout to tolerate wrapper prefixes.
_UDS_WRITE_PROBE_ALLOWED = "__sonic_mgmt_dni_uds_w_ok__"
_UDS_WRITE_PROBE_DENIED = "__sonic_mgmt_dni_uds_w_no__"

logger = logging.getLogger(__name__)
_NMAP_MISSING_MARKER = "__sonic_mgmt_dni_nmap_missing__"


# ── ss output parsing ────────────────────────────────────────────────────────

@dataclass
class ListenerEntry:
    """One row from ``ss -tulpn`` output."""
    protocol: str
    local_address: str
    port: int
    process: str
    raw_line: str


def parse_ss_listeners(ss_output: str) -> List[ListenerEntry]:
    """Parse ``ss -tulpn`` output into structured entries.

    Expected columns: Netid  State  Recv-Q  Send-Q  Local Address:Port  Peer Address:Port  Process
    Only rows in LISTEN state are returned.
    """
    entries: List[ListenerEntry] = []
    for line in ss_output.splitlines():
        if "LISTEN" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        protocol = parts[0]
        local_addr_port = parts[4]

        # Handle IPv6 bracket notation [::]:port and plain addr:port.
        # rpartition handles bare IPv6 (no brackets) too — the last ':' is the port.
        if ']:' in local_addr_port:
            addr, port_str = local_addr_port.rsplit(':', 1)
        else:
            addr, _, port_str = local_addr_port.rpartition(':')

        try:
            port = int(port_str)
        except ValueError:
            continue

        process = parts[-1] if 'users:' in parts[-1] else ""
        entries.append(ListenerEntry(
            protocol=protocol,
            local_address=addr,
            port=port,
            process=process,
            raw_line=line,
        ))
    return entries


def extract_listening_ports(entries: List[ListenerEntry]) -> Set[int]:
    """Return the set of port numbers from parsed listener entries."""
    return {e.port for e in entries}


# ── nmap output parsing ─────────────────────────────────────────────────────

def parse_nmap_open_ports(nmap_output: str) -> Set[int]:
    """Extract open TCP port numbers from nmap text output.

    Matches lines like: ``9339/tcp  open  ...``
    """
    ports: Set[int] = set()
    for line in nmap_output.splitlines():
        match = re.match(r"(\d+)/tcp\s+open", line)
        if match:
            ports.add(int(match.group(1)))
    return ports


def is_nmap_installed(engine: LinuxSshEngine) -> bool:
    """Return True when ``nmap`` is available on the scanner host."""
    output = engine.run_cmd(
        f"command -v nmap >/dev/null 2>&1 && nmap --version || echo {_NMAP_MISSING_MARKER}",
        validate=False,
    )
    installed = _NMAP_MISSING_MARKER not in output and "Nmap version" in output
    logger.info("nmap is %s on scanner host", "installed" if installed else "not installed")
    return installed


def install_nmap(engine: LinuxSshEngine) -> None:
    """Install ``nmap`` on the scanner host."""
    engine.run_cmd("sudo apt-get update && sudo apt-get install -y nmap", validate=True)


# ── Remote command helpers ───────────────────────────────────────────────────

# ``ss`` always prints a ``Netid State ...`` header on success. Asserting on it
# avoids treating a silent failure (binary missing, permission denied, …) as
# "no listeners", which would otherwise let port-exposure tests false-pass.
_SS_HEADER_MARKER = "Netid"


def _run_ss(engine: LinuxSshEngine, cmd: str) -> str:
    """Run an ``ss`` command with validation + output sanity check.

    Raises:
        RuntimeError: when stdout does not contain the standard ``ss`` header,
            i.e. the command silently produced unparseable output.
    """
    output = engine.run_cmd(cmd, validate=True)
    if _SS_HEADER_MARKER not in output:
        raise RuntimeError(
            f"_run_ss: unrecognized output from {cmd!r} "
            f"(missing {_SS_HEADER_MARKER!r} header). Output={output!r}"
        )
    return output


def run_ss_tulpn(engine: LinuxSshEngine) -> str:
    """Run ``sudo ss -a -tulpn`` on the DUT and return raw output."""
    return _run_ss(engine, "sudo ss -a -tulpn")


def run_ss_unix(engine: LinuxSshEngine) -> str:
    """Run ``sudo ss -x`` on the DUT and return raw output."""
    return _run_ss(engine, "sudo ss -x")


_IP_RE = re.compile(r'^[\d.:a-fA-F]+$')


def tcp_probe(engine: LinuxSshEngine, host: str, port: int, timeout: int = 3) -> bool:
    """Attempt a TCP connect from the DUT to *host*:*port*.

    Returns True if connection succeeded (port is open), False otherwise.
    Uses bash /dev/tcp to avoid nested Python quoting issues.
    """
    if not _IP_RE.match(host):
        raise ValueError(f"tcp_probe: invalid host {host!r}")
    cmd = f"timeout {timeout} bash -c 'echo > /dev/tcp/{host}/{port}' 2>/dev/null && echo OPEN || echo CLOSED"
    output = engine.run_cmd(cmd)
    return "OPEN" in output


def check_socket_exists(engine: LinuxSshEngine, path: str) -> bool:
    """Return True if *path* is a socket file on the DUT."""
    qpath = shlex.quote(path)
    output = engine.run_cmd(f"sudo test -S {qpath} && echo EXISTS || echo MISSING")
    return "EXISTS" in output


def check_can_write(engine: LinuxSshEngine, path: str) -> bool:
    """Return True if the connected user CAN write to *path*.

    A True result for a non-privileged user is a security failure condition.

    Uses ``if test -w …; then printf probe_ok; else printf probe_no`` so a
    failing ``test -w`` never prevents emitting a result (unlike
    ``cmd; echo $?`` under ``set -e``).  Probe strings are unique tokens, not
    substrings of one another.
    """
    qpath = shlex.quote(path)
    a, d = _UDS_WRITE_PROBE_ALLOWED, _UDS_WRITE_PROBE_DENIED
    cmd = (
        f"if test -w {qpath}; then printf '%s\\n' '{a}'; "
        f"else printf '%s\\n' '{d}'; fi"
    )
    output = engine.run_cmd(cmd)
    last = output.strip().splitlines()[-1].strip() if output.strip() else ""
    if last == a:
        return True
    if last == d:
        return False
    raise RuntimeError(
        f"check_can_write: unexpected remote output for {path!r} "
        f"(last line={last!r}, full={output!r})"
    )


def get_file_owner_and_perms(engine: LinuxSshEngine, path: str) -> Tuple[str, str]:
    """Return (owner, octal_permissions) for *path* on the DUT."""
    qpath = shlex.quote(path)
    output = engine.run_cmd(f"sudo stat -c '%U %a' {qpath}")
    parts = output.strip().split()
    if len(parts) >= 2:
        return parts[0], parts[1]
    return "", ""


# ── iptables helpers (TC-DNI-05) ─────────────────────────────────────────────

IPTABLES_BACKUP_PATH = "/tmp/iptables_backup_dni_test.txt"
IPTABLES_LEGACY_BACKUP_PATH = "/tmp/iptables_legacy_backup_dni_test.txt"


def _file_has_content(engine: LinuxSshEngine, path: str) -> bool:
    """Return True if *path* exists on the DUT and is non-empty."""
    output = engine.run_cmd(f"test -s {path} && echo OK || echo FAIL")
    return "OK" in output


def save_iptables(engine: LinuxSshEngine) -> None:
    """Backup iptables rules from both nft and legacy backends.

    On modern Debian-based DUTs ``iptables-save`` is a wrapper for
    ``iptables-nft-save``.  When the active rules live in the legacy backend
    the wrapper emits the warning ``iptables-legacy tables present, use
    iptables-legacy-save to see them`` to *stderr* and writes nothing to
    *stdout*, so a naive ``iptables-save > file`` produces an empty file.
    To remain backend-agnostic we save both backends and require at least one
    non-empty backup.

    Raises RuntimeError if **both** backups end up empty/missing.
    """
    engine.run_cmd(
        f"sudo iptables-save > {IPTABLES_BACKUP_PATH} 2>/dev/null || true"
    )
    engine.run_cmd(
        f"sudo iptables-legacy-save > {IPTABLES_LEGACY_BACKUP_PATH} 2>/dev/null || true"
    )

    nft_ok = _file_has_content(engine, IPTABLES_BACKUP_PATH)
    legacy_ok = _file_has_content(engine, IPTABLES_LEGACY_BACKUP_PATH)

    if not nft_ok and not legacy_ok:
        raise RuntimeError(
            "iptables backup empty for both nft and legacy backends: "
            f"{IPTABLES_BACKUP_PATH}, {IPTABLES_LEGACY_BACKUP_PATH}"
        )

    logger.info("iptables backup saved (nft=%s, legacy=%s)", nft_ok, legacy_ok)


def flush_iptables(engine: LinuxSshEngine) -> None:
    """Flush iptables rules from both backends and set default ACCEPT policy.

    Both ``iptables-legacy`` and ``iptables-nft`` are flushed because either
    backend may carry active rules and the alternative-managed ``iptables``
    wrapper only targets one of them.  Errors per-backend are suppressed so a
    missing/unused backend does not abort the whole flush.
    """
    for binary in ("iptables-legacy", "iptables-nft"):
        engine.run_cmd(
            f"(sudo {binary} -F && "
            f"sudo {binary} -X && "
            f"sudo {binary} -P INPUT ACCEPT && "
            f"sudo {binary} -P FORWARD ACCEPT && "
            f"sudo {binary} -P OUTPUT ACCEPT) 2>/dev/null || true"
        )


def restore_iptables(engine: LinuxSshEngine) -> None:
    """Restore iptables rules from saved backups, with one retry per backend.

    Each backend is restored only if its backup file is present and non-empty.
    On persistent failure, raises ``RuntimeError`` so callers do not silently
    leave the DUT with a flushed firewall.

    Raises:
        RuntimeError: when both restore attempts for a backend fail (non-zero
            exit code from iptables-restore / iptables-legacy-restore).
    """
    backups = (
        (IPTABLES_BACKUP_PATH, "sudo iptables-restore"),
        (IPTABLES_LEGACY_BACKUP_PATH, "sudo iptables-legacy-restore"),
    )

    for path, restore_cmd in backups:
        if not _file_has_content(engine, path):
            continue
        try:
            engine.run_cmd(f"{restore_cmd} < {path}", validate=True)
        except Exception:
            logger.error(
                "First restore attempt failed for %s, retrying...",
                path, exc_info=True,
            )
            try:
                engine.run_cmd(f"{restore_cmd} < {path}", validate=True)
            except Exception as exc:
                logger.error(
                    "Second restore attempt failed for %s; keeping backup file.",
                    path, exc_info=True,
                )
                raise RuntimeError(
                    f"Failed to restore iptables from {path} after 2 attempts; "
                    f"DUT firewall may be flushed. Backup retained for manual recovery."
                ) from exc
        engine.run_cmd(f"rm -f {path}")


def run_nmap(engine: LinuxSshEngine, target_ip: str) -> str:
    """Run a full TCP connect scan from *engine* against *target_ip*.

    Uses ``nmap -sT -p 1-65535 --open`` and returns raw output.

    """
    if not _IP_RE.match(target_ip):
        raise ValueError(f"run_nmap: invalid target_ip {target_ip!r}")
    output = engine.run_cmd(f"nmap -sT -p 1-65535 --open {target_ip}", validate=False)
    if "nmap: command not found" in output or "-bash: nmap: command not found" in output:
        raise RuntimeError(
            "run_nmap: nmap is not installed on scanner host. "
            "Run the verify_nmap_installed fixture before executing external scans."
        )
    missing = [m for m in ("Nmap scan report", "Nmap done") if m not in output]
    if missing:
        raise ValueError(
            f"run_nmap: nmap output for {target_ip!r} is missing required "
            f"marker(s) {missing}. Output={output!r}"
        )
    return output
