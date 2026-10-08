"""
Shared helper functions for BMC NVUE / bmc-cli tests.

Each helper that talks to the BMC accepts an ``interface`` parameter
("nvue" or "bmc_cli") where both backends exist, so coverage can be
parametrized later without a rewrite.
"""
import contextlib
import logging
import os
import re
import json
import shlex
import socket
import tempfile
import threading
import time
import uuid

import pytest
from netmiko.exceptions import NetmikoTimeoutException
try:  # ReadTimeout is a separate netmiko exception (not a NetmikoTimeoutException subclass)
    from netmiko.exceptions import ReadTimeout as NetmikoReadTimeout
except ImportError:  # older netmiko
    NetmikoReadTimeout = NetmikoTimeoutException

from ngts.nvos_constants.constants_nvos import FansConsts, ImageConsts, PlatformConsts
from ngts.nvos_tools.infra.BmcCliTool import BmcCliTool
from ngts.nvos_tools.infra.BmcTool import BmcTool, BMC_IP, dut_ping, ensure_usb0_up
from ngts.nvos_tools.infra.CurlTool import CurlTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import (
    OPENBMC, ROOT, get_cl_bmc_root_password,
)
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()

CL_REBOOT_SUCCESS_STRINGS = (
    'Action succeeded',
    'Power cycling',
    'System is undergoing',
    'Initiating immediate cold reboot',  # CL response for 'mode immediate'
)

NOS_REBOOT_SETTLE_SECONDS = 60
NOS_POWER_ON_TIMEOUT = 180

# Process-level latch: BMC suites only need ZTP off once per pytest process.
_ZTP_DISABLED_FOR_SESSION = False
_STALE_ARTIFACTS_SWEPT_FOR_SESSION = False

# On-DUT scratch files this suite creates, by prefix. Each is uuid-suffixed and
# removed by the call that made it; the sweep only catches what a crash left.
_DUT_SCRATCH_GLOBS = (
    '/tmp/ngts_bmc_internal_redfish_*',
    '/tmp/ngts_bmc_curl_*',
    '/tmp/ngts_bmc_reset_*',
)


def sweep_stale_dut_artifacts(engine):
    """Remove scratch files a previous run's crash left on the DUT.

    Every writer already removes its own files in a ``finally``, so this is
    only for the paths that skip it: a killed run, a lost SSH channel, or a
    host reboot mid-test. Sweeping at the start rather than in teardown is
    deliberate -- teardown does not run in exactly those cases. The files hold
    BMC inventory, so they should not sit in /tmp between runs.
    """
    global _STALE_ARTIFACTS_SWEPT_FOR_SESSION
    if _STALE_ARTIFACTS_SWEPT_FOR_SESSION:
        return
    _STALE_ARTIFACTS_SWEPT_FOR_SESSION = True
    found = engine.run_cmd(
        f"ls -1 {' '.join(_DUT_SCRATCH_GLOBS)} 2>/dev/null | wc -l",
        validate=False) or ''
    count = re.search(r'(\d+)', found)
    if count and int(count.group(1)):
        logger.info("Sweeping %s stale BMC scratch file(s) left on the DUT",
                    count.group(1))
    engine.run_cmd(f"sudo rm -f {' '.join(_DUT_SCRATCH_GLOBS)}", validate=False)


def ensure_ztp_disabled(engine):
    """Disable Cumulus ZTP if it is still enabled / in progress.

    ZTP wall broadcasts ("ZTP: Skipping DHCP refresh...") interleave into the
    SSH channel after NOS reboot and break brittle output parsers (e.g.
    ``/proc/uptime``). BMC suites do not need ZTP; turn it off once per process
    so reboot-heavy tests keep a clean channel. No-ops on platforms without ZTP.
    """
    global _ZTP_DISABLED_FOR_SESSION
    if _ZTP_DISABLED_FOR_SESSION:
        return
    try:
        has_ztp = engine.run_cmd(
            'test -x /usr/sbin/ztp && echo yes || echo no', validate=False)
        if 'yes' not in (has_ztp or ''):
            _ZTP_DISABLED_FOR_SESSION = True
            return
        status = engine.run_cmd('sudo /usr/sbin/ztp -s', validate=False) or ''
        if re.search(r'state:\s*disabled', status, re.IGNORECASE):
            logger.info("ZTP already disabled — leaving it alone")
            _ZTP_DISABLED_FOR_SESSION = True
            return
        logger.info(
            "Disabling ZTP for BMC suite (status was: %s)",
            ' '.join(status.split())[:120])
        # NVUE first (product path); fall back to ztp -d / -a.
        out = engine.run_cmd(
            'nv action disable system ztp force', validate=False) or ''
        if 'Action succeeded' not in out and 'succeeded' not in out.lower():
            engine.run_cmd('sudo /usr/sbin/ztp -a', validate=False)
            out = engine.run_cmd('sudo /usr/sbin/ztp -d', validate=False) or ''
        status_after = engine.run_cmd(
            'sudo /usr/sbin/ztp -s', validate=False) or ''
        if re.search(r'state:\s*disabled', status_after, re.IGNORECASE):
            logger.info("ZTP disabled for this pytest session")
            _ZTP_DISABLED_FOR_SESSION = True
        else:
            logger.warning(
                "ZTP disable may not have stuck (status: %s; last out: %s)",
                ' '.join(status_after.split())[:120],
                ' '.join(out.split())[:120])
    except Exception as err:
        logger.warning(
            "Could not ensure ZTP is disabled (%s: %s) — continuing",
            type(err).__name__, err)


def _get_bmc_ping_ip(topology_obj=None):
    """Return the BMC IP to ping for reachability checks.

    CL platforms use usb0 at ``BMC_IP``; IB/NVOS platforms use BmcTool.BMC_LOCAL_IP.
    When ``topology_obj`` is omitted, fall back to ``BMC_IP`` (CL usb0 default).
    """
    if topology_obj is None:
        return BMC_IP
    is_cumulus = topology_obj.players.get('dut', {}).get('is_cumulus', False)
    if is_cumulus:
        return BMC_IP
    return BmcTool.BMC_LOCAL_IP


# usb0 Redfish GET (SN11). Default matches shipping CL 5.19: HTTP :443, no -k.
# HTTPS opt-in is a run setting (--bmc-internal-redfish-scheme=https + -k).
# Do not dual-probe. Do not use CurlTool (OOB/SN49).
BMC_INTERNAL_REDFISH_SCHEME_DEFAULT = "http"
BMC_INTERNAL_REDFISH_PORT_DEFAULT = "443"
BMC_INTERNAL_REDFISH_ETH0_PATH = (
    "/redfish/v1/Managers/BMC_0/EthernetInterfaces/eth0"
)


def get_bmc_internal_redfish_settings():
    """Return (scheme, port) for the usb0 Redfish GET.

    Pytest ``--bmc-internal-redfish-scheme`` / ``--bmc-internal-redfish-port``
    win via env export in ``pytest_configure``. Else env, else shipping HTTP.
    """
    scheme = (
        os.environ.get("BMC_INTERNAL_REDFISH_SCHEME") or
        BMC_INTERNAL_REDFISH_SCHEME_DEFAULT
    ).strip().lower()
    port = str(
        os.environ.get("BMC_INTERNAL_REDFISH_PORT") or
        BMC_INTERNAL_REDFISH_PORT_DEFAULT
    ).strip()
    if scheme not in ("https", "http"):
        raise ValueError(
            f"BMC_INTERNAL_REDFISH_SCHEME must be https or http, got {scheme!r}"
        )
    return scheme, port


def internal_redfish_get(engine, path=BMC_INTERNAL_REDFISH_ETH0_PATH):
    """GET Redfish on the CL cpu–BMC usb0 link. One scheme only.

    SSH to CL lands in the mgmt VRF; usb0 is in the default VRF, so this
    runs ``sudo ip vrf exec default curl --interface usb0``. HTTPS uses
    curl ``insecure``; HTTP does not.

    Credentials go in a 0600 curl ``-K`` config copied onto the DUT. They
    must not appear on curl argv (no ``-u user:password``).
    """
    scheme, port = get_bmc_internal_redfish_settings()
    if not path.startswith("/"):
        path = "/" + path
    username, password = BmcTool.get_bmc_credentials(engine)
    url = f"{scheme}://{BMC_IP}:{port}{path}"
    # One token per GET for all three on-DUT artifacts. Fixed names meant the
    # response body outlived the call, so a curl that failed left the previous
    # call's body in place for the extract step below to summarize -- asserting
    # against a stale response instead of failing. They also held BMC inventory
    # in world-readable /tmp indefinitely, which undercuts keeping the
    # credentials out of argv via the -K config.
    token = uuid.uuid4().hex
    out_file = f"/tmp/ngts_bmc_internal_redfish_{token}.json"
    sum_file = f"/tmp/ngts_bmc_internal_redfish_{token}.summary.json"
    cfg_name = f"ngts_bmc_curl_{token}.cfg"
    cfg_remote = f"/tmp/{cfg_name}"
    # Write the body on-DUT, then cat a tiny summary. Echoing the full
    # Redfish document through the SSH parser truncates it; embedding JSON
    # in the python -c argv makes netmiko treat the real output as echo.
    extract = (
        "import json;"
        "d=json.load(open(%r));"
        "s={'Id': d.get('Id'), 'odata': d.get('@odata.id'),"
        "'nkeys': len(d)};"
        "fp=open(%r,'w'); json.dump(s, fp); fp.write('\\n'); fp.close()"
    ) % (out_file, sum_file)
    cfg_lines = [
        f"user = {json.dumps(f'{username}:{password}')}",
        "silent",
        "show-error",
        'noproxy = "*"',
        'interface = "usb0"',
        'max-time = "30"',
        f"output = {json.dumps(out_file)}",
        f"url = {json.dumps(url)}",
    ]
    if scheme == "https":
        cfg_lines.append("insecure")
    cfg_local = None
    raw = None
    copied = False
    try:
        with tempfile.NamedTemporaryFile(
                "w", delete=False, encoding="utf-8") as tmp:
            tmp.write("\n".join(cfg_lines) + "\n")
            cfg_local = tmp.name
        os.chmod(cfg_local, 0o600)
        engine.copy_file(source_file=cfg_local, file_system="/tmp",
                         dest_file=cfg_name, direction="put")
        copied = True
        engine.run_cmd(f"sudo chmod 600 {shlex.quote(cfg_remote)}")
        curl_cmd = (
            f"sudo ip vrf exec default curl -K {shlex.quote(cfg_remote)}"
        )
        with allure.step(f"usb0 Redfish GET {scheme}://{BMC_IP}:{port}{path}"):
            logger.info(
                "usb0 Redfish GET via curl -K %s (no -u on argv)", cfg_remote)
            engine.run_cmd(curl_cmd)
            engine.run_cmd(f"sudo python3 -c {shlex.quote(extract)}")
            raw = engine.run_cmd(f"cat {shlex.quote(sum_file)}")
    finally:
        # Body and summary go with the config: they are only needed between the
        # curl and the read a few lines above.
        engine.run_cmd(
            f"sudo rm -f {shlex.quote(out_file)} {shlex.quote(sum_file)}",
            validate=False)
        if copied:
            engine.run_cmd(
                f"sudo rm -f {shlex.quote(cfg_remote)}", validate=False)
        if cfg_local:
            try:
                os.unlink(cfg_local)
            except OSError:
                pass
    start = (raw or "").find("{")
    end = (raw or "").rfind("}")
    body = None
    if start != -1 and end > start:
        try:
            body = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            body = None
    if not isinstance(body, dict) or not body:
        snippet = (raw or "")[:400]
        raise AssertionError(
            f"usb0 Redfish GET {scheme}://{BMC_IP}:{port}{path} "
            f"did not return JSON. raw[:400]={snippet!r}"
        )
    return body


def skip_if_no_bmc(devices, engines=None):
    """Skip test if device has no BMC. Optionally ensures usb0 is up.

    When ``engines`` is provided, also disable ZTP once per process so reboot
    tests are not interrupted by ZTP wall broadcasts on the SSH channel, and
    clear scratch files a previous run may have left on the DUT.
    """
    if not getattr(devices.dut, 'has_bmc', False):
        pytest.skip("Device does not have BMC")
    if engines is not None:
        sweep_stale_dut_artifacts(engines.dut)
        ensure_ztp_disabled(engines.dut)
        ensure_usb0_up(engines.dut)


def ignore_cl_reboot_result(result):
    """Handle CL reboot output that doesn't match IB/NVOS expected strings.

    CL returns 'Power cycling the switch... Action succeeded' instead of
    'Performing reboot' / 'System will be rebooted', causing a false-negative.
    """
    if isinstance(result, ResultObj) and not result.result:
        info = str(getattr(result, 'info', ''))
        if any(s in info for s in CL_REBOOT_SUCCESS_STRINGS):
            logger.info("Ignoring CL reboot false-negative ResultObj")
            result.ignore_result()


def wait_for_bmc_ready(engines, topology_obj=None, timeout=300, poll_interval=15):
    """Wait until the BMC is reachable and responding.

    Polls ping every poll_interval seconds until timeout. When ``topology_obj``
    is given, the ping target follows platform type (CL usb0 vs IB local BMC IP);
    otherwise ``BMC_IP`` is used. Checks usb0 each poll with ``strict=False`` so
    a temporarily-down link does not abort the wait (product path owns bring-up).
    """
    bmc_ip = _get_bmc_ping_ip(topology_obj)
    logger.info(f"Waiting up to {timeout}s for BMC at {bmc_ip} to become ready...")
    start = time.time()
    while time.time() - start < timeout:
        ensure_usb0_up(engines.dut, strict=False)
        if dut_ping(engines.dut, bmc_ip)[0]:
            logger.info("BMC is reachable at %s", bmc_ip)
            return
        logger.debug(f"BMC not yet reachable at {bmc_ip}, "
                     f"retrying in {poll_interval}s...")
        time.sleep(poll_interval)
    raise TimeoutError(
        f"BMC at {bmc_ip} did not become reachable within {timeout}s")


def wait_for_bmc_cli_auth(engines, timeout=300, poll_interval=10):
    """Wait until bmc-cli can authenticate.

    After an NVUE firmware install the BMC still pings but bmc-cli returns
    ``Login failed with error code: -4`` for a while. Activating via
    ``reset bmc-reset`` in that window fails the test. Poll ``info`` until
    it returns JSON, restoring usb0 each attempt.
    """
    start = time.time()
    last_err = None
    while time.time() - start < timeout:
        ensure_usb0_up(engines.dut, strict=False)
        try:
            info = BmcCliTool(engines.dut).system_info()
            if isinstance(info, dict) and info:
                logger.info(
                    "bmc-cli authenticates after %.0fs", time.time() - start)
                return
            last_err = f"non-dict info: {type(info).__name__}"
        except Exception as err:
            last_err = f"{type(err).__name__}: {err}"
            logger.info(
                "bmc-cli not ready (%s); retry in %ss", last_err, poll_interval)
        time.sleep(poll_interval)
    raise TimeoutError(
        f"bmc-cli did not authenticate within {timeout}s: {last_err}")


def validate_bmc_healthy(engines, interface="nvue"):
    """Assert BMC inventory state is 'ok'."""
    inventory = get_bmc_inventory(engines, interface=interface)
    state = inventory.get(PlatformConsts.INV_STATE)
    assert state == FansConsts.STATE_OK, \
        f"BMC state is '{state}', expected '{FansConsts.STATE_OK}'"


def wait_for_bmc_healthy(engines, interface="nvue", timeout=300,
                         poll_interval=10):
    """Poll until BMC inventory state is 'ok', then return.

    Use this instead of ``validate_bmc_healthy`` when the BMC has just been
    reset or reflashed. ``wait_for_bmc_ready`` is not enough on its own: the
    BMC answers ICMP long before bmcweb is serving, and on a measured
    ``bmc-cli reset bmc-reset`` it pinged immediately while Redfish -- and so
    the inventory -- took 68s to return. A ping followed by a single assert is
    therefore a race, and on BMC 88.0070.1904 it loses.

    A one-shot assertion is still right for the callers that have not just
    disturbed the BMC, which is why this is a separate helper.
    """
    deadline = time.time() + timeout
    state = None
    while True:
        try:
            state = get_bmc_inventory(
                engines, interface=interface).get(PlatformConsts.INV_STATE)
        except Exception as ex:
            # Unreadable rather than unhealthy: NVUE cannot report inventory
            # until the CPU->BMC Redfish path is back.
            logger.debug(f"BMC inventory not readable yet: {ex}")
            state = None
        if state == FansConsts.STATE_OK:
            logger.info(f"BMC inventory state is '{state}'")
            return
        if time.time() >= deadline:
            break
        time.sleep(poll_interval)
    raise AssertionError(
        f"BMC state is '{state}', expected '{FansConsts.STATE_OK}' "
        f"after waiting {timeout}s"
    )


def wait_for_bmc_down(engines, interface="nvue", timeout=120,
                      poll_interval=5):
    """Wait until the BMC stops reporting healthy, i.e. the reset really landed.

    Pair this with ``wait_for_bmc_healthy`` around a reset, because the
    recovery poll on its own can return on its very first call: a reset
    command comes back before the BMC has actually dropped, so the inventory
    is still answering 'ok' from the pre-reset BMC and the case would pass
    without any recovery having been observed. SN12 met the same edge on usb0
    -- the disconnect lands several seconds after the call returns -- and
    solved it the same way, by waiting for the down edge before the up edge.

    An unreadable inventory counts as down: once the CPU->BMC Redfish path
    goes, NVUE cannot answer at all.
    """
    deadline = time.time() + timeout
    while True:
        try:
            state = get_bmc_inventory(
                engines, interface=interface).get(PlatformConsts.INV_STATE)
        except Exception as ex:
            logger.info(f"BMC inventory unreadable, treating as down: {ex}")
            return
        if state != FansConsts.STATE_OK:
            logger.info(f"BMC left '{FansConsts.STATE_OK}', now '{state}'")
            return
        if time.time() >= deadline:
            break
        time.sleep(poll_interval)
    raise AssertionError(
        f"BMC still reports '{FansConsts.STATE_OK}' {timeout}s after the "
        f"reset, so it never went down and a recovery cannot be observed"
    )


def cl_build_serial(engine) -> tuple:
    """Return the running CL build as a tuple, e.g. ``(5, 19, 0, 25)``.

    Read from ``/etc/image-release``, not ``/etc/lsb-release``: the latter
    carries only ``DISTRIB_RELEASE=5.19.0`` and so cannot tell one 5.19 RC from
    another, which is the whole point when a case has to pick its expectation by
    the release a feature landed in. Compared as a tuple, so a branch build on an
    older line (mlx-6810-01 runs 5.18.6809.0015) orders below 5.19.0.0025.
    """
    raw = str(engine.run_cmd(
        "grep -m1 '^IMAGE_BUILD_SERIAL_ID=' /etc/image-release", validate=False))
    match = re.search(r'IMAGE_BUILD_SERIAL_ID="?([0-9][0-9.]*)', raw)
    assert match, f"Could not read IMAGE_BUILD_SERIAL_ID from: {raw[:200]}"
    return tuple(int(part) for part in match.group(1).split('.'))


def get_bmc_inventory(engines, interface="nvue"):
    """Return BMC inventory as a parsed dict.

    NVUE: ``nv show platform inventory BMC -o json``
    bmc-cli: ``bmc-cli system-info --output json``
    """
    if interface == "nvue":
        return OutputParsingTool.parse_json_str_to_dictionary(
            Platform().inventory.show("BMC")).get_returned_value()
    else:
        return BmcCliTool(engines.dut).system_info()


def get_bmc_firmware(engines, interface="nvue"):
    """Return BMC firmware info as a parsed dict.

    NVUE: ``nv show platform firmware BMC -o json``
    bmc-cli: ``bmc-cli firmware-list --output json``
    """
    if interface == "nvue":
        return OutputParsingTool.parse_json_str_to_dictionary(
            Platform().firmware.show("BMC")).get_returned_value()
    else:
        return BmcCliTool(engines.dut).firmware_list()


def console_target(dut_engine):
    """Return the serial-console mux target ('cpu' / 'bmc') as bmc-cli reports it.

    ``bmc-cli console get`` prints a short banner; the target is on the
    ``Current output:`` line.
    """
    output = BmcCliTool(dut_engine).console_get() or ""
    for line in output.splitlines():
        if 'current output' in line.lower():
            return line.split(':', 1)[1].strip().lower()
    return output.strip().lower()


SOL_SSH_PORT = 2200
SOL_SERVICE = 'obmc-console@ttyS2.service'
SOL_SSH_SOCKET = 'obmc-console-ssh.socket'
SOL_DOMAIN_SOCKET = 'obmc-console@ttyS2.socket'
SOL_RESTORE_MARKER = 'BMC_CONSOLE_MUX_NOT_RESTORED'


@contextlib.contextmanager
def bmc_host_console_capture(engines, topology_obj):
    """Capture the host's serial console from the BMC (Serial-over-LAN).

    Yields a callable returning everything captured so far.

    Three conditions must all hold or the BMC sees nothing: the console mux must
    point at ``bmc``, both :data:`SOL_SERVICE` and :data:`SOL_SSH_SOCKET` must be
    running, and the console must be read over ssh on :data:`SOL_SSH_PORT`.
    Attaching a local ``obmc-console-client`` instead captures zero bytes, and
    ``/var/log/obmc-console.log`` is not an alternative either -- no ``logfile``
    is configured, so the server writes nothing to disk.

    A reboot inside the block kills the caller's DUT session, so the mux is
    restored on a reconnected engine.
    """
    import paramiko

    oob_ip = bmc_redfish_oob_host(engines, topology_obj)
    original_target = console_target(engines.dut)
    bmc = open_bmc_oob_shell(oob_ip)
    chunks = []
    stop_reading = threading.Event()
    session = channel = reader = None

    def bmc_systemctl(args):
        # These units deadlock on stop: the console server conflicts with
        # serial-getty@ttyS2 and its ExecStopPost starts it, so systemd waits out
        # the 90s stop timeout. Bound every call.
        return bmc.run_cmd(f"timeout 45 systemctl {args}", timeout=60)

    try:
        with allure.step("Arm BMC serial-over-LAN capture of the host console"):
            bmc_systemctl(f"start {SOL_SERVICE}")
            bmc_systemctl(f"start {SOL_SSH_SOCKET}")
            BmcCliTool(engines.dut).console_set('bmc')
            time.sleep(3)
            last_err = None
            for password in (get_cl_bmc_root_password(), OPENBMC):
                try:
                    session = paramiko.SSHClient()
                    session.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    session.connect(oob_ip, port=SOL_SSH_PORT, username=ROOT,
                                    password=password, look_for_keys=False,
                                    allow_agent=False, timeout=40)
                    break
                except Exception as err:
                    last_err = f"{type(err).__name__}: {err}"
                    session = None
            endpoint = '{}:{}'.format(oob_ip, SOL_SSH_PORT)
            assert session, (f"Could not open the BMC serial-over-LAN session on "
                             f"{endpoint} (last: {last_err})")
            channel = session.invoke_shell(width=200, height=60)

        def drain():
            while not stop_reading.is_set():
                try:
                    if channel.recv_ready():
                        chunks.append(channel.recv(65535).decode(errors='ignore'))
                        continue
                except Exception:
                    break
                time.sleep(0.5)

        # Drain continuously: nothing reads the channel while the caller waits on
        # the reboot, and a full receive window would stall the BMC mid-boot.
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        yield lambda: ''.join(chunks)
    finally:
        stop_reading.set()
        if reader is not None:
            reader.join(timeout=10)
        for closeable in (channel, session):
            try:
                if closeable is not None:
                    closeable.close()
            except Exception:
                pass
        try:
            _restore_console_target(engines, original_target)
        except Exception as err:
            logger.error(f"[{SOL_RESTORE_MARKER}] could not put the console mux back to "
                         f"'{original_target}' ({type(err).__name__}: {err}) — the BMC may "
                         f"still own the external console port on this setup")
        try:
            bmc_systemctl(f"stop {SOL_SSH_SOCKET}")
            bmc_systemctl(f"stop {SOL_DOMAIN_SOCKET} {SOL_SERVICE}")
            time.sleep(3)
            bmc_systemctl(f"start {SOL_DOMAIN_SOCKET}")
            bmc_systemctl(f"reset-failed {SOL_SERVICE}")
        except Exception as err:
            logger.warning(f"Could not fully restore the BMC console units: "
                           f"{type(err).__name__}: {err}")
        bmc.disconnect()


def _restore_console_target(engines, target):
    """Put the console mux back, on a revived DUT session.

    ``reconnect_engine`` is a ``run_cmd('')`` retry loop, so it costs nothing on
    a session that is still alive. It runs unconditionally rather than as a
    fallback: a reboot taken under capture usually kills the caller's session,
    and one path that always executes beats a rare branch that never does.
    """
    DutUtilsTool.reconnect_engine(engines.dut, find_prompt_tries=12, find_prompt_delay=5)
    BmcCliTool(engines.dut).console_set(target)
    current = console_target(engines.dut)
    assert current == target, \
        f"Console mux left at '{current}', expected '{target}'"


def oob_driver_engine(engines):
    """Return an engine that survives a host reset, to drive OOB commands from.

    Prefer ``engines.sonic_mgmt`` (jump host / container) so the NOS can be
    observed going down and coming back. Fall back to ``engines.dut`` (fire and
    forget — the connection drops as the host resets).
    """
    return getattr(engines, 'sonic_mgmt', None) or engines.dut


def bmc_redfish_oob_host(engines, topology_obj):
    """Return the BMC OOB IPv4 to use for raw Redfish/curl.

    Raw Redfish only works against the BMC's OOB management interface on CL — the
    internal usb0 IP (``BMC_IP``) does NOT serve HTTPS:443 (curl
    hangs there; only ``bmc-cli`` reaches usb0). Falls back to ``BMC_IP`` if the
    OOB address is not populated in NOGA, in which case callers should still treat
    an empty Redfish response as "unavailable" and skip.
    """
    try:
        oob_ip = BmcTool.get_bmc_ip_addresses(engines, topology_obj).get("IPv4")
        if oob_ip:
            return oob_ip
    except Exception as err:
        logger.warning(f"Could not resolve BMC OOB IP ({err}) — falling back to {BMC_IP}")
    return BMC_IP


def make_oob_curl_tool(engines, topology_obj):
    """Build a ``CurlTool`` pointed at the BMC's external (OOB) management IP.

    Returns ``(curl_tool, oob_ip)``. Uses ``BmcTool.get_bmc_ip_addresses`` to
    resolve the OOB IPv4 from NOGA and ``BmcTool.get_bmc_credentials`` for auth.
    """
    with allure.step("Resolve BMC OOB IP and credentials"):
        oob_ip = BmcTool.get_bmc_ip_addresses(engines, topology_obj).get("IPv4")
        assert oob_ip, "BMC OOB IPv4 address not available (check the bmc_ip box in NOGA)"
        username, password = BmcTool.get_bmc_credentials(engines.dut)
        logger.info(f"BMC OOB IP: {oob_ip}")
        return CurlTool(server_host=oob_ip, username=username, password=password), oob_ip


# ---------------------------------------------------------------------------
# BMC OOB shell (dropbear) -- runner -> BMC management IP, not NOS -> usb0.
# Used for recovery / oracle when the internal CPU-to-BMC SSH hop is unavailable.
# ---------------------------------------------------------------------------


class RawBmcSsh:
    """Minimal plain-paramiko SSH wrapper for the BMC OOB (dropbear) shell.

    Why not ``LinuxSshEngine``: that engine goes through the netmiko/proxy path,
    which fails to authenticate against the BMC's dropbear sshd ("Authentication
    to device failed") and then runs a password-rotation fallback that tries wrong
    old passwords ('0penBmc'/'NvidiaR0cks!') -> repeated "Permission denied". Plain
    ``ssh root@<oob>`` works, so we mirror it here: a single clean paramiko connect
    with password + keyboard-interactive fallback, no rotation/hardening logic.

    Exposes just ``run_cmd`` and ``disconnect``.
    """

    def __init__(self, ip, username, password, timeout=30):
        import paramiko
        self.ip = ip
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # look_for_keys/allow_agent off => go straight to password; paramiko falls
        # back to keyboard-interactive automatically (what the BMC dropbear wants).
        self._client.connect(ip, username=username, password=password,
                             look_for_keys=False, allow_agent=False, timeout=timeout)

    def run_cmd(self, cmd, validate=False, timeout=60, **kwargs):
        _stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode(errors='ignore')
        err = stderr.read().decode(errors='ignore')
        return out + err

    def disconnect(self):
        try:
            self._client.close()
        except Exception:
            pass


def open_bmc_oob_shell(oob_ip):
    """Open an SSH shell to the BMC over its OOB management IP.

    The BMC's sshd is reachable on its OOB/iLO interface (e.g. ``<host>-ilo``),
    NOT on the internal usb0 address (``BMC_IP``) -- usb0 only carries
    bmc-cli/Redfish, so SSH'ing NOS -> ``BMC_IP`` is not a supported path.

    Uses a plain paramiko session (:class:`RawBmcSsh`) rather than
    ``LinuxSshEngine``. Tries the known BMC root passwords in turn, each as a
    fresh clean connection.
    """
    last_err = None
    for pw in (get_cl_bmc_root_password(), OPENBMC):
        try:
            eng = RawBmcSsh(ip=oob_ip, username=ROOT, password=pw)
            probe = eng.run_cmd('echo __bmc_ok__ ; obmcutil state', validate=False)
            if '__bmc_ok__' in (probe or '') or 'CurrentPowerState' in (probe or ''):
                logger.info(f"Opened BMC OOB shell to {oob_ip} as {ROOT}")
                return eng
            eng.disconnect()
            last_err = f"connected but unexpected output: {str(probe)[:120]}"
        except Exception as err:
            last_err = f"{type(err).__name__}: {err}"
        logger.info(f"BMC OOB connect attempt failed ({oob_ip}): {last_err}")
    raise AssertionError(
        f"Could not SSH to the BMC OOB IP {oob_ip} with known root passwords "
        f"(last: {last_err})")


def resolve_redfish_system_id(curl_tool, dut_engine=None):
    """Resolve the platform-specific Redfish System id via ``GET /Systems/``.

    Never assume the literal string ``system`` — different platforms name the
    host System differently (e.g. ``System_0``). Returns the basename of the
    first Members ``@odata.id``.
    """
    with allure.step("Resolve Redfish System id via GET /Systems/"):
        resp = curl_tool.run_redfish_command(rest_op='GET', path='/Systems/', dut_engine=dut_engine)
        data = OutputParsingTool.parse_json_str_to_dictionary(resp).get_returned_value()
        members = data.get('Members', []) if isinstance(data, dict) else []
        assert members, f"No Redfish Systems returned by GET /Systems/: {resp}"
        odata_id = members[0].get('@odata.id', '')
        system_id = odata_id.rstrip('/').rsplit('/', 1)[-1]
        assert system_id, f"Could not parse a System id from '{odata_id}'"
        logger.info(f"Resolved Redfish System id: {system_id}")
        return system_id


def redfish_action_accepted(response):
    """Best-effort check that a Redfish action POST was accepted by the BMC.

    Success is a 204 No Content (empty body) or any body without a Redfish
    error object. A failed action returns a top-level ``"error"`` property.
    """
    if response is None or not str(response).strip():
        return True
    return '"error"' not in str(response).lower()


def reset_nos_via_redfish(curl_tool, reset_type, system_id, dut_engine=None):
    """Reset the host / NOS via Redfish ``ComputerSystem.Reset`` (never the Manager).

    Args:
        curl_tool: A ``CurlTool`` pointed at the BMC (OOB IP for over-the-network).
        reset_type: One of ``GracefulRestart``, ``ForceRestart``, ``ForceOff``,
            ``On``, ``GracefulShutdown``.
        system_id: The platform System id (resolve with ``resolve_redfish_system_id``).
        dut_engine: Engine to run curl from (use a host that survives the reset).

    Returns the raw Redfish response text.
    """
    with allure.step(f"Redfish ComputerSystem.Reset '{reset_type}' on System '{system_id}'"):
        response = curl_tool.run_redfish_command(
            rest_op='POST',
            data=f'{{"ResetType": "{reset_type}"}}',
            path=f'/Systems/{system_id}/Actions/ComputerSystem.Reset',
            dut_engine=dut_engine)
        logger.info(f"ComputerSystem.Reset '{reset_type}' response: {response!r}")
        return response


def get_redfish_power_state(curl_tool, system_id, dut_engine=None):
    """Return the host System ``PowerState`` (e.g. 'On' / 'Off') via Redfish."""
    resp = curl_tool.run_redfish_command(
        rest_op='GET', path=f'/Systems/{system_id}', dut_engine=dut_engine)
    data = OutputParsingTool.parse_json_str_to_dictionary(resp).get_returned_value()
    return data.get('PowerState', '') if isinstance(data, dict) else ''


def wait_for_redfish_power_state(curl_tool, system_id, expected_state,
                                 dut_engine=None, timeout=180, poll_interval=10):
    """Poll the host System ``PowerState`` until it equals ``expected_state``."""
    with allure.step(f"Wait for System '{system_id}' PowerState == '{expected_state}'"):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = get_redfish_power_state(curl_tool, system_id, dut_engine=dut_engine)
            if last.lower() == expected_state.lower():
                logger.info(f"System PowerState is '{last}'")
                return True
            logger.debug(f"PowerState='{last}', waiting for '{expected_state}'...")
            time.sleep(poll_interval)
        raise TimeoutError(
            f"System '{system_id}' PowerState did not reach '{expected_state}' "
            f"within {timeout}s (last='{last}')")


def nos_uptime_seconds(engine, tries=3, delay=2):
    """Return the NOS host uptime in seconds (drops back near 0 after a reboot).

    After reboot, ZTP / wall broadcasts often interleave into the SSH channel and
    bury the ``/proc/uptime`` line. Search for the ``<idle> <idle>`` float pair
    rather than assuming the first token is clean, and retry briefly if needed.
    """
    # /proc/uptime is "<seconds> <idle-seconds>"; tolerate interleaved wall text.
    uptime_re = re.compile(r'(\d+(?:\.\d+)?)\s+\d+(?:\.\d+)?')
    last_out = ''
    for attempt in range(1, tries + 1):
        last_out = engine.run_cmd('cat /proc/uptime', validate=False) or ''
        match = uptime_re.search(last_out)
        if match:
            return float(match.group(1))
        logger.warning(
            "Could not parse NOS uptime (attempt %s/%s) from %r",
            attempt, tries, last_out[:300])
        if attempt < tries:
            time.sleep(delay)
    return None


def snapshot_bmc_state_safe(engines, tries=20, delay=15):
    """Tolerant BMC inventory + firmware snapshot for before/after comparisons.

    Gates on usb0 carrier + BMC ping (``wait_for_bmc_ready``) and retries, so a
    transient "Error: The requested item does not exist" / ``state=None`` while
    the BMC is still recovering from a reset does not fail the caller.

    Returns ``{'state', 'firmware_version', 'inventory'}``.
    """
    try:
        wait_for_bmc_ready(engines)
    except Exception as err:
        logger.info(f"wait_for_bmc_ready did not confirm BMC link "
                    f"({type(err).__name__}: {err}) — continuing to poll inventory")
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            ensure_usb0_up(engines.dut, strict=False)
            inventory = get_bmc_inventory(engines)
            state = inventory.get(PlatformConsts.INV_STATE)
            if state:
                firmware = get_bmc_firmware(engines)
                return {'state': state,
                        'firmware_version': firmware.get(PlatformConsts.FW_ACTUAL),
                        'inventory': inventory}
            logger.info(f"BMC inventory not populated yet (attempt {attempt}/{tries})")
        except Exception as err:
            last_err = err
            logger.info(f"BMC not ready for snapshot (attempt {attempt}/{tries}): "
                        f"{type(err).__name__}: {err}")
        time.sleep(delay)
    raise AssertionError(f"BMC did not return a readable inventory state after {tries} "
                         f"attempts (~{tries * delay}s). last error: {last_err}")


def wait_for_nos_reboot(engines, uptime_before=None, settle=NOS_REBOOT_SETTLE_SECONDS,
                        find_prompt_tries=90, find_prompt_delay=10):
    """Wait for the NOS to reboot and come back, then optionally prove it rebooted.

    Replaces ``DutUtilsTool.wait_on_system_reboot``'s port-22 "CLOSED" shutdown
    detection, which is unreliable on fast CL reboots (the port may never be seen
    closed between polls). Instead: disconnect, sleep for the reboot, reconnect,
    restore usb0. If ``uptime_before`` is given, assert ``/proc/uptime`` dropped
    (the authoritative proof the host actually rebooted).
    """
    with allure.step("Wait for the NOS to reboot and reconnect"):
        try:
            engines.dut.disconnect()
        except Exception:
            pass
        time.sleep(settle)
        DutUtilsTool.reconnect_engine(engines.dut, find_prompt_tries=find_prompt_tries,
                                      find_prompt_delay=find_prompt_delay)
        # Non-strict: product ifup may still be settling after NOS reboot.
        ensure_usb0_up(engines.dut, strict=False)
    if uptime_before is not None:
        with allure.step("Assert NOS uptime reset (host actually rebooted)"):
            uptime_after = nos_uptime_seconds(engines.dut)
            assert uptime_after is not None, (
                "Could not read NOS /proc/uptime after reconnect "
                "(SSH output likely polluted by wall/ZTP messages)"
            )
            assert uptime_after < uptime_before, (
                "NOS uptime did not reset ({:.0f}s -> {:.0f}s) — "
                "host does not appear to have rebooted".format(
                    uptime_before, uptime_after)
            )
            return uptime_after


def bmc_recover_nos(engines, topology_obj):
    """BMC-based NOS recovery (no external PDU / remote-reboot).

    The BMC is the reboot mechanism on these platforms, so recovery goes through
    it — never through a Noga ``remote_reboot`` PDU entry (which does not exist
    here and makes ``recover_dut_with_remote_reboot`` fail with "Reboot command is
    empty"). Cautious: only power the host if the NOS is actually unreachable.

    1. Reconnect to the NOS; if it answers, do nothing.
    2. Otherwise power the host on/reset via Redfish ``ComputerSystem.Reset`` over
       the BMC OOB, wait for PowerState 'On', then reconnect.
    """
    with allure.step("Recovery: reconnect to the NOS"):
        try:
            engines.dut.disconnect()
        except Exception:
            pass
        try:
            DutUtilsTool.reconnect_engine(engines.dut, find_prompt_tries=30, find_prompt_delay=10)
            ensure_usb0_up(engines.dut, strict=False)
            if (engines.dut.run_cmd('hostname', validate=False) or '').strip():
                logger.info("NOS is reachable again — no BMC power action needed")
                return
        except Exception as err:
            logger.info(f"NOS still unreachable ({type(err).__name__}: {err}) — "
                        f"recovering via the BMC")

    with allure.step("Recovery: power the host back on via the BMC (OOB Redfish)"):
        try:
            driver = oob_driver_engine(engines)
            curl_tool, _ = make_oob_curl_tool(engines, topology_obj)
            system_id = resolve_redfish_system_id(curl_tool, dut_engine=driver)
            power = get_redfish_power_state(curl_tool, system_id, dut_engine=driver)
            logger.info(f"BMC reports host PowerState: {power}")
            reset_type = "ForceRestart" if power.lower() == "on" else "On"
            reset_nos_via_redfish(curl_tool, reset_type, system_id, dut_engine=driver)
            try:
                wait_for_redfish_power_state(curl_tool, system_id, "On", dut_engine=driver,
                                             timeout=NOS_POWER_ON_TIMEOUT)
            except Exception as err:
                logger.warning(f"host PowerState did not confirm 'On' ({err})")
        except Exception as err:
            logger.warning(f"BMC-based recovery failed: {type(err).__name__}: {err}")

    with allure.step("Recovery: reconnect to the NOS after the BMC power action"):
        time.sleep(NOS_REBOOT_SETTLE_SECONDS)
        try:
            DutUtilsTool.reconnect_engine(engines.dut, find_prompt_tries=90, find_prompt_delay=10)
            ensure_usb0_up(engines.dut, strict=False)
        except Exception as err:
            logger.warning(f"reconnect after BMC recovery failed: {type(err).__name__}: {err}")


# ---------------------------------------------------------------------------
# BMC firmware update (burn) helpers.
#
# Two interchangeable transports drive the same update on CL/SPC6:
#   bmc_cli : bmc-cli firmware --file <image> ; bmc-cli reset bmc-reset
#   nvue    : nv action fetch / nv action install platform firmware BMC
# Both consume a '.fwpkg' staged on the switch by ensure_bmc_image_staged().
# ---------------------------------------------------------------------------

CL_BMC_LOCAL_IMAGE_DIR = "/home/cumulus/bmc_images"


def resolve_latest_fwpkg(engine, base_url, directory_path):
    """Resolve the latest .fwpkg filename from an HTTP directory listing.

    The ``lastrc_*`` directories on the build server rotate as new builds land,
    so the exact filename changes. Fetches the directory index and picks the
    correct ``.fwpkg`` (excluding ``*transition*``).

    Returns:
        tuple: (full_path, filename, version_name) or None if not found.
    """
    url = f'{base_url}{directory_path}'
    listing = engine.run_cmd(f'curl -s {shlex.quote(url)}', validate=False)
    matches = re.findall(r'href="([^"]*\.fwpkg)"', listing)
    candidates = [f for f in matches if 'transition' not in f.lower()]
    if not candidates:
        logger.warning(f"No .fwpkg files found in {url}")
        return None
    filename = sorted(candidates)[-1]
    full_path = directory_path.rstrip('/') + '/' + filename
    version_match = re.search(r'(\d+\.\d+\.\d+-\d+)', filename)
    version_name = version_match.group(1) if version_match else filename.replace('.fwpkg', '')
    logger.info(f"Resolved latest firmware: {filename} (version: {version_name})")
    return full_path, filename, version_name


def get_fw_info(engine, version):
    """Get firmware info (path, filename, version_name, base_url) for a given version.

    Entries with a ``directory`` field (rolling lastrc paths) resolve the actual
    filename from the HTTP directory listing at runtime; entries with a static
    ``path`` field return the values directly from the JSON.

    Skips when the platform defines no BMC firmware catalog. SN6800 and SN6810
    leave ``fw_versions_json_file_path`` unset on purpose -- the AST2600 images
    in the SN6600 catalog are not a validated pair for those boards and
    installing one leaves a BMC that will not boot -- and without this gate
    ``get_fw_component_version_dict()`` reaches ``open(None)``.
    """
    device = TestToolkit.get_device()
    if not (BmcTool.FW_VERSIONS_JSON_FILE or
            getattr(device, 'fw_versions_json_file_path', None)):
        pytest.skip("No BMC firmware catalog is defined for this platform")
    fw_dict = BmcTool.get_fw_component_version_dict("bmc", version)
    base_url = fw_dict.get('base_url')
    if 'directory' in fw_dict:
        resolved = resolve_latest_fwpkg(engine, base_url, fw_dict['directory'])
        assert resolved, f"Could not resolve .fwpkg from {base_url}{fw_dict['directory']}"
        return resolved[0], resolved[1], resolved[2], base_url
    return fw_dict['path'], fw_dict.get('filename') or fw_dict['path'].rsplit('/', 1)[-1], \
        fw_dict['version_name'], base_url


def bmc_image_version_name(image_path):
    """Extract a dotted version (e.g. '88.0060.2112') from a BMC image filename."""
    match = re.search(r'(\d+\.\d+\.\d+)', image_path)
    return match.group(1) if match else image_path.rsplit('/', 1)[-1]


def _parse_fw_version(version_str):
    """Parse a dotted firmware version (e.g. '88.0060.2112') into a comparable tuple."""
    return tuple(int(p) for p in re.findall(r'\d+', version_str or ''))


def _remote_file_size(engine, remote_path):
    """Return the size in bytes of ``remote_path`` on the switch, or None if absent."""
    output = engine.run_cmd(f'stat -c %s {shlex.quote(remote_path)}', validate=False)
    for line in reversed((output or '').splitlines()):
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def ensure_bmc_image_staged(engines, version):
    """Ensure the BMC image for ``version`` is on the switch, uploading it if missing.

    The firmware tests must run against a freshly-provisioned switch, so the image
    is uploaded from the release path in the FW versions JSON instead of relying on
    a manual pre-stage. An image already present with the expected size is reused.

    ``bmc-cli firmware --file`` only accepts the packaged ``.fwpkg``, and a secured
    (``rel-prod`` ERoT) switch only accepts production-signed images -- a debug-signed
    one is rejected with "security keys used to sign the image has been revoked".
    Since both variants share a filename and size, the staging directory carries the
    signing variant so a stale debug image is never mistaken for a prod-signed one.

    Returns:
        str: Absolute path of the staged ``.fwpkg`` on the switch.
    """
    path, filename, version_name, _base_url = get_fw_info(engines.dut, version)
    signing_variant = path.rstrip('/').rsplit('/', 2)[-2]
    staged_dir = f'{CL_BMC_LOCAL_IMAGE_DIR}/{signing_variant}'
    staged_path = f'{staged_dir}/{filename}'

    with allure.step(f"Ensure BMC image {version_name} is staged at {staged_path}"):
        assert os.path.isfile(path), \
            f"BMC firmware image not readable on the test host: {path}"
        expected_size = os.path.getsize(path)

        staged_size = _remote_file_size(engines.dut, staged_path)
        if staged_size == expected_size:
            logger.info(f"BMC image already staged ({staged_size} bytes): {staged_path}")
            return staged_path
        if staged_size is not None:
            logger.warning(f"Staged image is {staged_size} bytes but release image is "
                           f"{expected_size} - re-uploading {staged_path}")

        logger.info(f"Uploading BMC image to switch: {path} -> {staged_path}")
        engines.dut.run_cmd(f'mkdir -p {shlex.quote(staged_dir)}')
        engines.dut.copy_file(source_file=path, file_system=staged_dir,
                              dest_file=filename, direction='put')

        uploaded_size = _remote_file_size(engines.dut, staged_path)
        assert uploaded_size == expected_size, \
            (f"Failed to stage BMC image at {staged_path}: expected {expected_size} bytes, "
             f"got {uploaded_size}")

    return staged_path


CL_FETCH_SUCCESS_STRINGS = ('Firmware downloaded successfully', 'Action succeeded')


def _check_fetch_result(result, label):
    """Return True if fetch succeeded, False if it failed recoverably, else raise.

    NOTE: Check the failure markers before the success strings. ``ResultObj.info``
    echoes the expected-string list as text (which contains 'Action succeeded'),
    so testing CL_FETCH_SUCCESS_STRINGS first yields false positives.
    """
    if not (isinstance(result, ResultObj) and not result.result):
        return True
    info = str(getattr(result, 'info', ''))
    if 'SCP download failed' in info or ('sshpass' in info and 'No such file' in info):
        logger.warning(f"{label} fetch failed (sshpass unavailable on switch). Info: {info}")
        result.ignore_result()
        return False
    # These markers only ever appear in real device output, never in the
    # expected-string list, so they safely distinguish a failure from an echo.
    fetch_failure_markers = (
        'Name or service not known',  # DUT cannot resolve the remote host
        'FailedToCreate',             # NVUE could not create/download the file
        'Action failed',              # generic NVUE action failure
    )
    if any(marker in info for marker in fetch_failure_markers):
        logger.warning(f"{label} fetch failed (device reported an error). Info: {info}")
        result.ignore_result()
        return False
    if any(s in info for s in CL_FETCH_SUCCESS_STRINGS):
        logger.info(f"{label} fetch succeeded")
        result.ignore_result()
        return True
    result.verify_result()  # raises on any other failure
    return True


def fetch_bmc_firmware(url, interface="nvue", base_url=None):
    """Fetch (download) BMC firmware onto the switch via ``nv action fetch``.

    Fetch order:
        1. On-switch copy (primary): if the ``.fwpkg`` is already staged under
           ``CL_BMC_LOCAL_IMAGE_DIR/<signing_variant>/`` (same layout as
           ``ensure_bmc_image_staged``) or the flat ``CL_BMC_LOCAL_IMAGE_DIR/``,
           copy it to ``/tmp`` and fetch ``file://``.
        2. Upload from the test host's ``/auto`` NFS path, then fetch ``file://``.
        3. SCP from the build server (legacy) -- requires DUT-side name resolution.
        4. HTTP from ``base_url`` (last resort).

    The switch does not mount ``/auto`` and cannot resolve the build server
    hostname, so a remote ``scp://...`` fetch fails with "Name or service not
    known"; the ``file://`` paths avoid DUT-side resolution entirely.

    Args:
        url: Absolute path to the firmware file on the local/NFS server.
        interface: "nvue" or "bmc_cli" (bmc-cli combines fetch+install).
        base_url: HTTP fallback base URL, used only if the other paths fail.
    """
    if interface != "nvue":
        logger.info("bmc-cli combines fetch+install; skipping standalone fetch")
        return

    engine = TestToolkit.get_engine()
    filename = url.rsplit('/', 1)[-1]
    dut_local_path = f'/tmp/{filename}'

    # Prefer the signing-variant path used by ensure_bmc_image_staged()
    # (CL_BMC_LOCAL_IMAGE_DIR/<variant>/<filename>), then the flat legacy path.
    signing_variant = url.rstrip('/').rsplit('/', 2)[-2] if '/' in url else None
    onswitch_candidates = []
    if signing_variant and signing_variant != filename:
        onswitch_candidates.append(f'{CL_BMC_LOCAL_IMAGE_DIR}/{signing_variant}/{filename}')
    onswitch_candidates.append(f'{CL_BMC_LOCAL_IMAGE_DIR}/{filename}')

    # Each candidate is attempted independently so a transient failure on the
    # first one does not skip the remaining on-switch paths.
    for onswitch_src in onswitch_candidates:
        try:
            ls_out = engine.run_cmd(f'ls -l {shlex.quote(onswitch_src)}', validate=False)
            if 'No such file' in ls_out:
                continue
            logger.info(f"Image already staged on switch: {onswitch_src} -> {dut_local_path}")
            engine.run_cmd(f'cp -f {shlex.quote(onswitch_src)} {shlex.quote(dut_local_path)}')
            result_onswitch = Platform().firmware.bmc.action_fetch(
                dut_local_path, base_url='file://')
            if _check_fetch_result(result_onswitch, "file:// (on-switch)"):
                return
        except Exception as e:
            logger.warning(f"On-switch fetch from {onswitch_src} failed "
                           f"({type(e).__name__}: {e}); trying next source")

    try:
        logger.info(f"Uploading firmware from local path to switch: {url} -> {dut_local_path}")
        engine.copy_file(source_file=url, file_system='/tmp', dest_file=filename,
                         direction='put')
        logger.info(f"Fetching from switch-local copy: file://{dut_local_path}")
        result_local = Platform().firmware.bmc.action_fetch(dut_local_path, base_url='file://')
        if _check_fetch_result(result_local, "file://"):
            return
    except Exception as e:
        logger.warning(f"Local upload + file:// fetch failed "
                       f"({type(e).__name__}: {e}); falling back to SCP from build server")

    scp_base = ImageConsts.SCP_PATH
    logger.info(f"Attempting SCP fetch: {scp_base}{url}")
    result_scp = Platform().firmware.bmc.action_fetch(url, base_url=scp_base)
    if _check_fetch_result(result_scp, "SCP"):
        return

    if base_url is None:
        raise RuntimeError(
            f"Local upload and SCP fetch failed, and no HTTP fallback URL provided for {url}")
    logger.info(f"Falling back to HTTP fetch: {base_url}{url}")
    result_http = Platform().firmware.bmc.action_fetch(url, base_url=base_url)
    _check_fetch_result(result_http, "HTTP")


_EROT_BUSY_MAX_RETRIES = 8

# Install errors meaning "the BMC/ERoT is not ready to start an update yet", as
# opposed to a hard image/auth failure. Waiting out the ERoT background copy
# (never a BMC reset) clears them.
_INSTALL_RETRIABLE_ERRORS = (
    'ERoT is busy',
    'Timed out',
    'not able to enter into update mode',
    'enter into update mode',
)


def _is_retriable_install_error(text):
    """True if the install output indicates a transient 'BMC not ready' state."""
    return any(sig in (text or '') for sig in _INSTALL_RETRIABLE_ERRORS)


# ERoT background-copy gating.
#
# After a BMC firmware install + BMC reset, the ERoT copies the freshly-activated
# image into the *other* slot. Until that copy finishes, the inactive slot reports
# FirmwareState == 'ImageCopyInProgress' and the BMC rejects any new install with
# "Device is not able to enter into update mode". CRITICAL: resetting the BMC while
# the copy is in progress RESTARTS the copy, so a busy state must be waited out --
# never "recovered" with a BMC reset. Observed copy time is ~5-7 minutes.
_EROT_COPY_IN_PROGRESS = 'ImageCopyInProgress'
_EROT_COPY_TIMEOUT = 900       # max wait for one background copy to finish
_EROT_COPY_POLL_INTERVAL = 30
_EROT_STATE_UNREADABLE_BUDGET = 90   # give up once the state stays unreadable this long


def _bmc_inactive_slot_state(engines):
    """Return the BMC InactiveFirmwareSlot.FirmwareState, or None if unreadable.

    Read from bmc-cli ``firmware-detail`` -- the NVUE firmware view does not expose
    the per-slot ERoT copy state that gates a subsequent update.
    """
    try:
        detail = BmcCliTool(engines.dut).firmware_list()
        return (detail.get('Oem', {}).get('Nvidia', {})
                .get('InactiveFirmwareSlot', {}).get('FirmwareState'))
    except Exception as err:
        logger.warning(f"Could not read BMC inactive-slot state "
                       f"({type(err).__name__}: {err})")
        return None


def wait_for_erot_copy_idle(engines, timeout=_EROT_COPY_TIMEOUT,
                            poll_interval=_EROT_COPY_POLL_INTERVAL):
    """Wait until the ERoT background copy finishes before the next update.

    Polls the BMC inactive-slot state until it is no longer
    ``ImageCopyInProgress``. A no-op (returns immediately) when the ERoT is
    already idle, so it is safe to call before every install. Never resets the
    BMC -- that would restart the copy. Returns True if idle, False otherwise.

    An unreadable state is not the same as a copy in progress. ``bmc-cli
    firmware-detail`` can return non-JSON (seen while the BMC is still coming up
    after a failed install), and polling that for the full timeout only burns
    time -- it never becomes readable on its own. So a run of unreadable polls is
    capped at ``_EROT_STATE_UNREADABLE_BUDGET``; the install path already retries
    if the ERoT does turn out to be busy.
    """
    start = time.time()
    unreadable_since = None
    while time.time() - start < timeout:
        ensure_usb0_up(engines.dut, strict=False)
        state = _bmc_inactive_slot_state(engines)
        if state is None:
            if unreadable_since is None:
                unreadable_since = time.time()
            unreadable_for = time.time() - unreadable_since
            if unreadable_for >= _EROT_STATE_UNREADABLE_BUDGET:
                logger.warning(
                    f"BMC inactive-slot state unreadable for {unreadable_for:.0f}s -- "
                    "cannot tell whether an ERoT copy is running, so not waiting out the "
                    f"remaining {timeout - (time.time() - start):.0f}s")
                return False
            logger.info(f"BMC inactive-slot state unreadable; retrying in {poll_interval}s")
        else:
            unreadable_since = None
            if state != _EROT_COPY_IN_PROGRESS:
                logger.info(f"ERoT background copy idle (inactive slot state: {state})")
                return True
            logger.info(f"ERoT background copy in progress (state={state}); "
                        f"waiting {poll_interval}s...")
        time.sleep(poll_interval)
    logger.warning(f"ERoT background copy did not go idle within {timeout}s")
    return False


def _settle_bmc_for_retry(engines):
    """Wait out a transient 'cannot enter update mode' / ERoT-busy state before retry.

    The cause is the ERoT background copy from the *previous* install still
    running. Resetting the BMC RESTARTS that copy and wedges the state, so this
    waits for the copy to finish instead of resetting. Tolerant: never raises.
    """
    logger.info("Install not ready -> waiting for the ERoT background copy to finish "
                "(NOT resetting the BMC -- a reset restarts the copy)")
    try:
        wait_for_bmc_ready(engines, timeout=300)
    except Exception as err:
        logger.warning(f"BMC not ready before retry: {type(err).__name__}: {err}")
    wait_for_erot_copy_idle(engines)
    ensure_usb0_up(engines.dut, strict=False)


def recover_bmc_after_failed_install(engines, topology_obj=None):
    """Recover BMC health after a failed firmware install (no PDU/remote_reboot).

    On BMC platforms there is no external PDU, so ``recover_dut_with_remote_reboot``
    fails with "Reboot command is empty". A failed install leaves the NOS up but the
    BMC possibly busy/stuck; resetting the BMC clears it. Tolerant: never raises.
    """
    with allure.step("Recover BMC after failed install (reset + wait, no PDU)"):
        try:
            ensure_usb0_up(engines.dut, strict=False)
            wait_for_bmc_cli_auth(engines, timeout=180)
            BmcCliTool(engines.dut).reset_bmc()
        except Exception as err:
            logger.warning(f"BMC reset during recovery failed: {type(err).__name__}: {err}")
        try:
            wait_for_bmc_ready(engines, topology_obj, timeout=480)
        except Exception as err:
            logger.warning(f"BMC not ready during recovery: {type(err).__name__}: {err}")
        ensure_usb0_up(engines.dut, strict=False)


# Extra wait after Netmiko loses the install session: the burn may still be
# running. We must confirm completion before any BMC reboot.
_INSTALL_POST_TIMEOUT_VERIFY = 600
_INSTALL_POST_TIMEOUT_POLL = 20


def _nvue_action_running(engines):
    """True if an NVUE action is running, False if idle, None if unreadable."""
    try:
        out = engines.dut.run_cmd('nv show action', validate=False) or ''
    except Exception as err:
        logger.warning(f"nv show action failed ({type(err).__name__}: {err})")
        return None
    return 'state: running' in out


def _recent_bmc_install_outcome(engines):
    """Best-effort journal scan: 'succeeded', 'failed', or None if unknown."""
    try:
        out = engines.dut.run_cmd(
            "sudo journalctl --since '30 min ago' --no-pager 2>/dev/null | "
            "grep -E 'install platform firmware BMC|Action succeeded|Action failed' | "
            "tail -80",
            validate=False,
        ) or ''
    except Exception as err:
        logger.warning(f"Could not read recent install journal "
                       f"({type(err).__name__}: {err})")
        return None

    # Prefer the latest definitive marker that appears after a BMC-install line.
    saw_bmc_install = False
    last_outcome = None
    for line in out.splitlines():
        if 'install platform firmware BMC' in line:
            saw_bmc_install = True
            last_outcome = None  # reset; wait for this install's outcome
        if not saw_bmc_install:
            continue
        if 'Action succeeded' in line:
            last_outcome = 'succeeded'
        elif 'Action failed' in line:
            last_outcome = 'failed'
    return last_outcome


def _inactive_slot_version(engines):
    """Return InactiveFirmwareSlot.Version, or '' if unreadable."""
    try:
        detail = BmcCliTool(engines.dut).firmware_list()
        return (detail.get('Oem', {}).get('Nvidia', {})
                .get('InactiveFirmwareSlot', {}).get('Version') or '')
    except Exception as err:
        logger.warning(f"Could not read inactive-slot version "
                       f"({type(err).__name__}: {err})")
        return ''


def _verify_nvue_install_after_session_loss(engines, expected_version=None,
                                            timeout=_INSTALL_POST_TIMEOUT_VERIFY,
                                            poll_interval=_INSTALL_POST_TIMEOUT_POLL):
    """Confirm the NVUE BMC install finished after Netmiko timed out/disconnected.

    Fail-closed: returns True only with strong evidence the install completed.
    Never treats the timeout itself as success -- callers must not reboot the
    BMC until this returns True.
    """
    with allure.step("Verify BMC install completed after session loss (no reboot yet)"):
        logger.warning(
            "Install session timed out/disconnected -- waiting for NVUE action to "
            "finish and confirming outcome before any BMC reboot")
        start = time.time()
        while time.time() - start < timeout:
            running = _nvue_action_running(engines)
            if running is True:
                logger.info("NVUE action still running; waiting before reboot...")
                time.sleep(poll_interval)
                continue
            if running is None:
                # Connection flaky after timeout; keep polling rather than reboot.
                time.sleep(poll_interval)
                continue

            outcome = _recent_bmc_install_outcome(engines)
            if outcome == 'succeeded':
                logger.info("Post-timeout verification: Action succeeded confirmed")
                return True
            if outcome == 'failed':
                logger.error("Post-timeout verification: Action failed confirmed")
                return False

            # Inactive slot already carrying the target version is also strong
            # evidence the burn finished (staged, not yet activated).
            if expected_version:
                inactive = _inactive_slot_version(engines)
                if inactive and expected_version in inactive:
                    logger.info(f"Post-timeout verification: inactive slot already "
                                f"has target version '{inactive}'")
                    return True

            logger.info("NVUE action idle but install outcome not yet confirmed; "
                        f"waiting {poll_interval}s...")
            time.sleep(poll_interval)

        logger.error(
            f"Could not confirm BMC install success within {timeout}s after "
            "session loss -- failing closed (will NOT reboot BMC)")
        return False


def _send_install_command(engines, install_cmd, expected_version=None):
    """Send the NVUE install command and wait for a definitive result.

    ``nv action install platform firmware BMC files ...`` prompts
    "Do you want to continue? [y/N]" and blocks until answered, then prints
    "Action executing..." for ~9-12 minutes during the burn. Send the command,
    catch the confirmation prompt, answer 'y', then wait (up to 15 min) for the
    burn to finish. Without answering 'y' the read hangs forever on the prompt.

    ERoT busy handling: after a BMC flash the ERoT security chip runs a background
    copy lasting several minutes, and any install started during that window is
    rejected. Retriable rejections are retried up to ``_EROT_BUSY_MAX_RETRIES``
    times so the caller need not know how long the background copy takes.

    Returns:
        True  -- 'Action succeeded' seen (or post-timeout verification confirmed it).
        False -- definitive failure, or session loss without confirmable success
                 (fail-closed: never reboot on an ambiguous timeout).
    """
    engine = engines.dut
    for attempt in range(1, _EROT_BUSY_MAX_RETRIES + 1):
        logger.info(f"Sending BMC install command "
                    f"(attempt {attempt}/{_EROT_BUSY_MAX_RETRIES}): {install_cmd}")
        try:
            # With 'force' the install does NOT print a [y/N] prompt -- it streams
            # "Action executing ..." for ~8-12 min and then "Action succeeded". Use
            # a full-burn read_timeout so we wait for the real result; a [y/N] prompt
            # (non-force path) still returns early because expect_string matches it.
            output = engine.engine.send_command(
                install_cmd,
                expect_string=r'\[y/N\]|\[Y/n\]|Action succeeded|Action failed',
                read_timeout=960,
            )
            # Only answer the confirmation if a prompt actually appeared and no
            # terminal result was seen yet (avoid sending a spurious 'y').
            if (re.search(r'\[[yY]/[nN]\]', output) and
                    'Action succeeded' not in output and 'Action failed' not in output):
                output += engine.engine.send_command(
                    'y',
                    expect_string=r'Action succeeded|Action failed',
                    read_timeout=960,
                )
            tail = output[-300:] if len(output) > 300 else output
            logger.info(f"Install command returned: {tail}")

            if 'Action failed' in output:
                if _is_retriable_install_error(output):
                    if attempt < _EROT_BUSY_MAX_RETRIES:
                        logger.warning(f"BMC not ready to install (attempt {attempt}/"
                                       f"{_EROT_BUSY_MAX_RETRIES}): {tail}. Waiting for the "
                                       f"ERoT copy before retry...")
                        _settle_bmc_for_retry(engines)
                        continue
                    logger.error(f"ERoT/BMC still not ready after "
                                 f"{_EROT_BUSY_MAX_RETRIES} attempts")
                    return False
                logger.error(f"BMC install action failed (non-retriable): {tail}")
                return False

            return 'Action succeeded' in output

        except (socket.error, NetmikoTimeoutException, NetmikoReadTimeout, OSError) as e:
            # Classify by exception TYPE, never by message: the ReadTimeout message
            # echoes our expect_string (which contains the literal text "Action
            # failed"), so string-matching it would falsely report a failure.
            # Do NOT treat timeout as success -- verify the install finished before
            # any BMC reboot (a mid-burn reboot is unsafe).
            logger.warning(f"Install command timed out or disconnected "
                           f"({type(e).__name__}: {str(e)[:120]}). Verifying install "
                           f"completion before any BMC reboot.")
            return _verify_nvue_install_after_session_loss(
                engines, expected_version=expected_version)
        except Exception as e:
            logger.warning(f"Install command raised unexpected {type(e).__name__}: "
                           f"{str(e)[:120]}. Verifying install completion before any "
                           f"BMC reboot.")
            return _verify_nvue_install_after_session_loss(
                engines, expected_version=expected_version)

    return False  # exhausted all retries


def _reboot_bmc_and_wait(engines, bmc_reboot_timeout=480, poll_interval=15):
    """Reboot the BMC via bmc-cli and wait for it to come back.

    On SPC6/Chameleon the install stages firmware to inactive flash but does NOT
    reboot the BMC; a manual BMC reboot is required to activate it. The BMC takes
    ~3-8 minutes to come back (longer after firmware activation).
    """
    logger.info("Rebooting BMC via bmc-cli to activate new firmware")
    ensure_usb0_up(engines.dut, strict=False)
    wait_for_bmc_cli_auth(engines, timeout=300)
    BmcCliTool(engines.dut).reset_bmc()

    logger.info(f"Waiting up to {bmc_reboot_timeout}s for BMC to come back...")
    time.sleep(60)
    start = time.time()
    usb0_restore_interval = 60
    last_usb0_restore = 0
    while time.time() - start < bmc_reboot_timeout:
        time.sleep(poll_interval)
        elapsed = time.time() - start
        if elapsed - last_usb0_restore >= usb0_restore_interval:
            logger.info("Restoring usb0 (BMC reboot drops the CPU-side interface)")
            ensure_usb0_up(engines.dut, strict=False)
            last_usb0_restore = elapsed
        if dut_ping(engines.dut, BMC_IP)[0]:
            logger.info("BMC is back after reboot; waiting for the ERoT background copy")
            ensure_usb0_up(engines.dut, strict=False)
            # Wait for the ERoT copy to actually finish rather than a blind sleep;
            # starting a new install while it runs fails with 'cannot enter update mode'.
            wait_for_erot_copy_idle(engines)
            return
        logger.debug("BMC not yet reachable, waiting...")

    raise TimeoutError(f"BMC did not come back within {bmc_reboot_timeout}s after reboot")


def install_bmc_firmware(engines, filename, topology_obj, test_name="",
                         interface="nvue", skip_reboot=False):
    """Install BMC firmware from an already-fetched file.

    The full CL/Chameleon update flow is:
        1. ``nv action install`` -- burns firmware to inactive flash (~9 min)
        2. ``bmc-cli reset bmc-reset`` -- reboots the BMC to activate it
        3. Wait for the BMC to come back and re-establish usb0

    With ``skip_reboot`` steps 2-3 are skipped, so the firmware is staged but not
    activated until the next BMC reboot.
    """
    if interface == "bmc_cli":
        return BmcCliTool(engines.dut).firmware_update(filename)

    # Gate on the ERoT background copy from any prior install being finished --
    # starting while it runs fails with "cannot enter update mode".
    wait_for_erot_copy_idle(engines)

    # 'force' and 'skip-reboot' are mutually exclusive on CL: combining them
    # always raises an uncaught exception in the ActionRunner.
    if skip_reboot:
        install_cmd = f'nv action install platform firmware BMC files {filename} skip-reboot'
    else:
        install_cmd = f'nv action install platform firmware BMC files {filename} force'

    # Pass the expected version so a post-timeout verifier can confirm the burn
    # via the inactive slot before any BMC reboot is attempted.
    succeeded = _send_install_command(
        engines, install_cmd, expected_version=bmc_image_version_name(filename))
    if not succeeded:
        raise RuntimeError(f"BMC firmware install command failed for {filename}")

    if not skip_reboot:
        _reboot_bmc_and_wait(engines)


def _is_downgrade(engines, image_path):
    """True when ``image_path`` is an older BMC version than the running one.

    Both the bmc-cli and the Redfish transports refuse to install an older
    version unless forced, so each asks this before deciding to force. An
    unreadable or unparseable version is reported as "not a downgrade": forcing
    on a guess is the more destructive mistake, and the caller's post-install
    version check catches a refusal anyway.
    """
    target_version = bmc_image_version_name(image_path)
    try:
        current_version = get_bmc_firmware(engines).get(PlatformConsts.FW_ACTUAL, '')
        current_tuple = _parse_fw_version(current_version)
        target_tuple = _parse_fw_version(target_version)
        if current_tuple and target_tuple and target_tuple < current_tuple:
            logger.info(f"Target {target_version} is older than current "
                        f"{current_version} -> forcing (downgrade)")
            return True
        logger.info(f"Target {target_version} is not older than current "
                    f"{current_version} -> not forcing")
    except Exception as err:
        logger.warning(f"Could not compare firmware versions ({err}); not forcing")
    return False


def update_bmc_firmware_via_bmc_cli(engines, image_path, wait_for_bmc=True):
    """Update BMC firmware via bmc-cli from a pre-staged local image, then reset the BMC.

    The bmc-cli counterpart of ``update_bmc_firmware_via_nvue``::

        bmc-cli firmware --file <image_path> [--force]   # stage/update firmware
        bmc-cli reset bmc-reset                          # activate by rebooting the BMC

    ``bmc-cli firmware`` refuses to install an OLDER version unless ``--force`` is
    given, so ``--force`` is passed only for a downgrade.

    Args:
        engines: Test engines (engines.dut is the CPU-side SSH engine).
        image_path: Absolute path to the .fwpkg already on the switch (see
            ``ensure_bmc_image_staged``); bmc-cli rejects raw .bin images.
        wait_for_bmc: If True, reset the BMC and wait for it to come back
            (activating the staged firmware); if False, stage only.
    """
    with allure.step(f"Verify BMC image exists on switch: {image_path}"):
        ls_out = engines.dut.run_cmd(f'ls -l {shlex.quote(image_path)}', validate=False)
        assert 'No such file' not in ls_out, \
            f"BMC firmware image not found on switch: {image_path} ({ls_out.strip()})"

    with allure.step("Wait for any prior ERoT background copy to finish"):
        # Starting while the ERoT copy runs fails with "cannot enter update mode".
        wait_for_erot_copy_idle(engines)

    with allure.step("Decide whether --force is needed (downgrade vs upgrade)"):
        force = _is_downgrade(engines, image_path)

    with allure.step(f"bmc-cli firmware --file {image_path}{' --force' if force else ''}"):
        # Retry a transient 'not ready to enter update mode' / ERoT-busy rejection
        # by waiting out the ERoT background copy (a BMC reset would restart it). A
        # hard failure (bad image/auth) is not retried -- the caller's version check
        # is the authoritative result.
        for attempt in range(1, _EROT_BUSY_MAX_RETRIES + 1):
            try:
                output = BmcCliTool(engines.dut).firmware(image_path, force=force)
            except Exception as err:
                output = f"{type(err).__name__}: {err}"
            logger.info(f"bmc-cli firmware update output (attempt {attempt}, tail): "
                        f"{str(output)[-300:]}")
            if 'completed successfully' in (output or ''):
                break
            if _is_retriable_install_error(output) and attempt < _EROT_BUSY_MAX_RETRIES:
                logger.warning(f"bmc-cli firmware not ready (attempt {attempt}/"
                               f"{_EROT_BUSY_MAX_RETRIES}); waiting for ERoT copy...")
                _settle_bmc_for_retry(engines)
                continue
            break

    if wait_for_bmc:
        with allure.step("bmc-cli reset bmc-reset (activate firmware) and wait for BMC"):
            _reboot_bmc_and_wait(engines)
    else:
        # Stage only: bmc-cli firmware writes inactive flash but does not
        # activate. Skipping reset mirrors NVUE skip-reboot -- the caller must
        # reset later if they want the new version to become active.
        logger.info("wait_for_bmc=False: leaving firmware staged (no bmc-cli reset)")


def update_bmc_firmware_via_nvue(engines, image_path, topology_obj, test_name="",
                                 skip_reboot=False):
    """Update BMC firmware via the NVUE action tree from a pre-staged local image.

    The NVUE counterpart of ``update_bmc_firmware_via_bmc_cli`` -- both take a
    local ``.fwpkg`` already on the switch (see ``ensure_bmc_image_staged``) so the
    firmware tests can run the *same* case over either transport::

        nv action fetch   platform firmware BMC file:///var/tmp/<image>
        nv action install platform firmware BMC files <image> [force|skip-reboot]

    The NVUE fetch only accepts ``file://`` sources under ``/tmp``, ``/var/tmp``,
    ``/home`` and ``/var/home``. In practice ``/var/tmp`` works reliably on SPC6
    while ``CL_BMC_LOCAL_IMAGE_DIR`` is not visible to the fetch action ("Local
    file not found"), so the image is copied into ``/var/tmp`` first.
    """
    filename = image_path.rsplit('/', 1)[-1]
    staged = f'/var/tmp/{filename}'
    with allure.step(f"Stage image into /var/tmp for NVUE fetch: {staged}"):
        engines.dut.run_cmd(f'cp {shlex.quote(image_path)} {shlex.quote(staged)}', validate=False)
        ls_out = engines.dut.run_cmd(f'ls -l {shlex.quote(staged)}', validate=False)
        assert 'No such file' not in ls_out, \
            f"Failed to stage BMC image at {staged}: {ls_out.strip()}"

    with allure.step(f"nv action fetch platform firmware BMC file://{staged}"):
        result = Platform().firmware.bmc.action_fetch(staged, base_url='file://')
        if not _check_fetch_result(result, "file://"):
            raise RuntimeError(f"NVUE fetch failed for {staged}")

    with allure.step(f"nv action install platform firmware BMC files {filename}"):
        install_bmc_firmware(engines, filename, topology_obj, test_name=test_name,
                             interface="nvue", skip_reboot=skip_reboot)


# Redfish firmware update. bmcweb answers the multipart POST with a Task and
# burns in the background, so the push and the wait are separate steps.
_REDFISH_TASK_TIMEOUT = 1800   # max wait for the background burn task
_REDFISH_TASK_POLL = 20
_REDFISH_TASK_DONE = ('Completed',)
_REDFISH_TASK_FAILED = ('Exception', 'Killed', 'Cancelled', 'Interrupted')


def _parse_redfish_body(response):
    """Parse a Redfish JSON body, returning ``{}`` for empty or non-JSON output.

    curl output reaches us through the SSH engine, so it can carry a trailing
    newline from ``-w`` or, when the request never got a reply, no JSON at all.
    """
    try:
        parsed = OutputParsingTool.parse_json_str_to_dictionary(response).get_returned_value()
    except Exception as err:
        logger.warning(f"Could not parse Redfish response ({err}): {str(response)[:200]}")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _redfish_task_path(response):
    """Return the Task path from a multipart-update response, without the service prefix.

    bmcweb replies to the push with the Task resource itself. ``CurlTool``
    prepends ``/redfish/v1`` to every path, so the absolute ``@odata.id`` is
    trimmed here rather than at each call site.
    """
    parsed = _parse_redfish_body(response)
    odata_id = parsed.get('@odata.id', '')
    if not odata_id:
        task_id = parsed.get('Id', '')
        assert task_id, \
            f"Redfish multipart update returned no task to track: {str(response)[:400]}"
        odata_id = f'/redfish/v1/TaskService/Tasks/{task_id}'
    prefix = '/redfish/v1'
    return odata_id[len(prefix):] if odata_id.startswith(prefix) else odata_id


def wait_for_redfish_task(curl_tool, task_path, dut_engine=None,
                          timeout=_REDFISH_TASK_TIMEOUT, poll_interval=_REDFISH_TASK_POLL):
    """Poll a Redfish Task to a terminal state and return its final body.

    Progress is logged on every poll so a stalled burn is visible in the run log
    while it happens, instead of only as a timeout half an hour later.
    """
    deadline = time.time() + timeout
    task = {}
    while time.time() < deadline:
        task = _parse_redfish_body(
            curl_tool.run_redfish_command(rest_op='GET', path=task_path, dut_engine=dut_engine))
        state = task.get('TaskState', '')
        logger.info(f"Redfish task {task_path}: state={state or 'unknown'} "
                    f"percent={task.get('PercentComplete')}")
        if state in _REDFISH_TASK_DONE:
            return task
        if state in _REDFISH_TASK_FAILED:
            raise AssertionError(
                f"Redfish firmware task ended in '{state}': {task.get('Messages') or task}")
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Redfish firmware task {task_path} did not finish within {timeout}s "
        f"(last state '{task.get('TaskState')}')")


def update_bmc_firmware_via_redfish(engines, image_path, topology_obj, wait_for_bmc=True):
    """Update BMC firmware over raw Redfish from a pre-staged local image.

    The Redfish counterpart of ``update_bmc_firmware_via_bmc_cli`` and
    ``update_bmc_firmware_via_nvue``. Those two reach the same ``UpdateService``
    underneath; this transport exercises the API directly, so a change in the
    Redfish contract cannot hide behind a CLI that absorbs it::

        POST /redfish/v1/UpdateService/update-multipart   -> Task
        GET  <task>                                       -> until Completed
        bmc-cli reset bmc-reset                           -> activate

    curl runs on the DUT but is aimed at the BMC's *OOB* address: usb0 does not
    serve HTTPS, so raw Redfish goes over OOB here as everywhere else in this
    suite. The package is read from the DUT's own filesystem, where
    ``ensure_bmc_image_staged`` already put it.

    ``wait_for_bmc=False`` leaves the firmware staged without the activating BMC
    reset, mirroring the bmc-cli transport.
    """
    with allure.step(f"Verify BMC image exists on switch: {image_path}"):
        ls_out = engines.dut.run_cmd(f'ls -l {shlex.quote(image_path)}', validate=False)
        assert 'No such file' not in ls_out, \
            f"BMC firmware image not found on switch: {image_path} ({ls_out.strip()})"

    with allure.step("Wait for any prior ERoT background copy to finish"):
        # Starting while the ERoT copy runs fails with "cannot enter update mode".
        wait_for_erot_copy_idle(engines)

    curl_tool, oob_ip = make_oob_curl_tool(engines, topology_obj)
    target = '/redfish/v1{}/{}'.format(PlatformConsts.BMC_FIRMWARE_INVENTORY_LINK,
                                       PlatformConsts.BMC_FIRMWARE_BMC_LINK)
    force = _is_downgrade(engines, image_path)

    with allure.step(f"Redfish multipart push of {image_path} to {oob_ip} (target {target})"):
        response = curl_tool.run_redfish_multipart_update(
            file_path=image_path, targets=[target], force_update=force,
            dut_engine=engines.dut)
        logger.info(f"Redfish multipart response (tail): {str(response)[-300:]}")
        task_path = _redfish_task_path(response)

    with allure.step(f"Wait for the Redfish firmware task to finish ({task_path})"):
        wait_for_redfish_task(curl_tool, task_path, dut_engine=engines.dut)

    if wait_for_bmc:
        with allure.step("bmc-cli reset bmc-reset (activate firmware) and wait for BMC"):
            _reboot_bmc_and_wait(engines)
    else:
        # Stage only: the burn writes inactive flash but does not activate, so
        # the caller must reset later for the new version to take effect.
        logger.info("wait_for_bmc=False: leaving firmware staged (no BMC reset)")
