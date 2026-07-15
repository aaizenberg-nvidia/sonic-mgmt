"""
Tool for driving the platform `SfpSysfsParser.get_supported_cable_ib_speed`
decoder directly against crafted `supported_cable_speed` values via the pmon
container.

Lets tests exercise the defensive N/A / unknown-bit / unparseable paths without
needing a special SDK build or an out-of-spec cable.
"""
import json
import logging

logger = logging.getLogger()


# Canonical negative / defensive-path input matrix.
# (input_value_or_None, expected_return, description)
#   None                    => sysfs key absent
#   'not-a-number'/'0x0'    => unparseable / empty bitmask
#   hex bitmask string      => value SfpSysfsParser would read from sysfs
NEGATIVE_CABLE_SPEED_CASES = [
    ("not-a-number", "N/A", "unparseable -> N/A + warning"),
    ("0x0", "N/A", "empty bitmask -> N/A (silent)"),
    ("0x80000000", "N/A", "only unknown bits -> N/A + warning"),
    ("0x100001", "sdr", "known(SDR_1X)+unknown -> sdr + warning"),
    ("0x7001000", "fdr,xdr", "real XDR cable (with unknown 0x4000000) -> fdr,xdr + warning"),
    ("0x3000000", "xdr", "XDR_1X|XDR_2X width collapse -> xdr"),
    ("0x81000000", "xdr", "known(XDR_1X)+unknown(0x80000000) -> xdr + warning"),
    (None, "N/A", "sysfs key absent -> N/A"),
]

_PMON_DRIVER_TEMPLATE = r'''
import json
from sonic_platform.sfp_sysfs_parser import SfpSysfsParser
CASES = __CASES__
p = SfpSysfsParser("/tmp/qg-mock-sysfs")
results = []
for raw, expected, desc in CASES:
    p.sysfs_module_data = {} if raw is None else {"supported_cable_speed": raw}
    got = p.get_supported_cable_ib_speed()
    results.append({"input": raw, "expected": expected, "got": got, "desc": desc})
print("QG_JSON:" + json.dumps(results))
'''

_QG_JSON_MARKER = "QG_JSON:"
_SYSLOG_WARNING_PATTERNS = "supported_cable_speed|Unrecognized bit|Unexpected value"
_SYSLOG_KEEP_TOKENS = ("pmon", "xcvrd", "WARNING", "supported_cable_speed")


def _looks_like_syslog_entry(line):
    lower = line.lower()
    return any(tok.lower() in lower for tok in _SYSLOG_KEEP_TOKENS)


class TransceiverSysfsTool:
    """Drives the SfpSysfsParser decoder inside pmon and inspects syslog for warnings."""

    DEFAULT_SYSLOG_MARKER = "QG supported-cable-ib-speed negative-path START"

    @staticmethod
    def drive_supported_cable_ib_speed_decoder(engine, cases=None):
        """Run each `(input, expected, desc)` case through SfpSysfsParser inside pmon.

        Returns a list of {"input", "expected", "got", "desc"} dicts, one per case.
        Raises AssertionError if pmon didn't emit the QG_JSON marker line.
        """
        cases = cases if cases is not None else NEGATIVE_CABLE_SPEED_CASES
        payload = _PMON_DRIVER_TEMPLATE.replace("__CASES__", repr(cases))
        cmd = "docker exec -i pmon python3 - <<'PY'\n" + payload + "\nPY"
        output = engine.run_cmd(cmd)
        marker_line = next(
            (ln for ln in output.splitlines() if ln.startswith(_QG_JSON_MARKER)), None)
        assert marker_line, (
            "pmon driver did not emit QG_JSON marker; SfpSysfsParser missing or "
            f"pmon unreachable. Raw output:\n{output}")
        return json.loads(marker_line[len(_QG_JSON_MARKER):])

    @staticmethod
    def mark_syslog(engine, marker=DEFAULT_SYSLOG_MARKER, tag="qg_supported_cable_ib_speed"):
        """Emit a sentinel line into /var/log/syslog so warnings can be sliced by time."""
        engine.run_cmd(f"sudo logger -t {tag} '{marker}'")
        return marker

    @staticmethod
    def grep_supported_cable_speed_warnings(engine, marker):
        """Return real syslog warning lines emitted after `marker`.

        Runs the awk|grep pipeline as root (syslog is owned by adm/root and not
        world-readable on many NVOS builds — see TestToolkit.get_loganalyzer_marker
        which uses `use_sudo=True` for the same reason). If /var/log/syslog is
        missing or unreadable even with sudo, raises RuntimeError so the caller
        surfaces the environment problem instead of a bogus "no warnings" pass.
        Filters out the shell echo of the pipeline itself so the caller only sees
        genuine syslog entries.
        """
        pipeline = (
            f"awk '/{marker}/{{seen=1; next}} seen' /var/log/syslog "
            f"| grep -iE '{_SYSLOG_WARNING_PATTERNS}' "
            "| tail -20"
        )
        precheck = engine.run_cmd(
            "sudo test -r /var/log/syslog && echo READABLE || echo UNREADABLE")
        if 'READABLE' not in precheck:
            raise RuntimeError(
                f"/var/log/syslog is not readable even with sudo on this setup: {precheck!r}")
        raw = engine.run_cmd(f'sudo sh -c "{pipeline}"')
        return [ln for ln in raw.splitlines()
                if _looks_like_syslog_entry(ln) and
                not ln.strip().startswith(('awk', '|', '>', 'sudo', 'sh'))]
