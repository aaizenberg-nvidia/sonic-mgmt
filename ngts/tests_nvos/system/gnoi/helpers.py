"""Helpers for the gNOI tests: counter reads, reachability probes, and host-artifact checks."""
import hashlib
import json
import logging
import os
import random
import re
import shlex
import subprocess
import tempfile
import time

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import HealthConsts, ImageConsts, PlatformConsts
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.infra.CmdRunner import CmdRunner
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.Firmware import PlatformComponent
from ngts.tests_nvos.platform.firmware_telemetry_helpers import canonicalize_fw_version
from ngts.tests_nvos.system.gnoi.constants import (
    TECHSUPPORT_EXPECTED_GNOI_ARTIFACTS,
    TECHSUPPORT_FORBIDDEN_GNOI_ARTIFACT,
    TECHSUPPORT_UNCOLLECTED_GNOI_ARTIFACTS,
    GnoicErr,
    GnoiErrorFlows,
    GnoiFileService,
    GnoiFirmware,
    GnoiOsService,
    GnoiPaths,
    GnoiRpcCounters,
    GnoiStaging,
)

logger = logging.getLogger()


def gnoi_rpc_failed(out, err) -> bool:
    """True when gnoic reported a failure on either stream (some builds print errors on stdout)."""
    merged = f'{out or ""}\n{err or ""}'
    return any(marker in merged for marker in GnoicErr.ALL_ERRS)


def gnoi_is_not_served(out, err) -> bool:
    """True when the endpoint did not answer at all, i.e. gNOI is not being served."""
    merged = f'{out or ""}\n{err or ""}'
    return any(marker in merged for marker in GnoicErr.NOT_SERVED)


def parse_gnoi_rpc_status(output):
    """
    Parse 'nv show system gnmi-server status gnoi-rpc [<rpc-name>]' JSON output.

    Returns a dict keyed by RPC name. A single-RPC show returns only that RPC's leaves, so it is
    normalized into the same {rpc-name: {leaves}} shape by the caller passing rpc_name.
    """
    parsed = OutputParsingTool.parse_json_str_to_dictionary(output).get_returned_value()
    return parsed if isinstance(parsed, dict) else {}


def _counter_value(entry, leaf) -> int:
    """A counter absent from the output is a zero counter, not a failure."""
    if not isinstance(entry, dict):
        return 0
    value = entry.get(leaf)
    return int(value) if value is not None else 0


def get_gnoi_rpc_counters(gnoi_rpc_component, rpc_name=None, dut_engine=None):
    """
    Return {rpc-name: (received, failed)}.

    With rpc_name, shows only that RPC. The collection may legitimately not list an RPC that has
    never been called, so missing entries read as (0, 0).
    """
    output = gnoi_rpc_component.show(op_param=rpc_name or '', dut_engine=dut_engine)
    parsed = parse_gnoi_rpc_status(output)

    # Single-RPC show returns the leaves at the top level rather than nested under the RPC name.
    if rpc_name and (GnoiRpcCounters.RECEIVED in parsed or GnoiRpcCounters.FAILED in parsed):
        parsed = {rpc_name: parsed}

    return {name: (_counter_value(entry, GnoiRpcCounters.RECEIVED),
                   _counter_value(entry, GnoiRpcCounters.FAILED))
            for name, entry in parsed.items()}


def get_gnoi_rpc_counter(gnoi_rpc_component, rpc_name, dut_engine=None):
    """Return (received, failed) for one RPC, reading the whole collection."""
    return get_gnoi_rpc_counters(gnoi_rpc_component, dut_engine=dut_engine).get(rpc_name, (0, 0))


def verify_gnoi_counter_leaves_are_numeric(gnoi_rpc_component, dut_engine=None):
    """The received/failed leaves are uint64, so every value present must parse as an integer."""
    parsed = parse_gnoi_rpc_status(gnoi_rpc_component.show(dut_engine=dut_engine))
    for rpc_name, entry in parsed.items():
        for leaf in (GnoiRpcCounters.RECEIVED, GnoiRpcCounters.FAILED):
            value = entry.get(leaf) if isinstance(entry, dict) else None
            if value is None:
                continue
            assert str(value).lstrip('-').isdigit() and int(value) >= 0, \
                f'{rpc_name}/{leaf} is not a uint64: {value!r}'


def read_gnoi_state_file(engine):
    """
    Return the parsed gNOI stats file, or {} when it is absent (gNMI server stopped).

    nv-gnoid writes the JSON without a trailing newline, and the ssh engine drops that last
    partial line, so the closing brace is lost unless a newline is echoed after the file.
    """
    output = engine.run_cmd(f'sudo cat {GnoiPaths.STATE_FILE} 2>/dev/null; echo')
    if not output or not output.strip():
        return {}
    try:
        return json.loads(output)
    except ValueError:
        # A truncated read is a real finding, not something to swallow silently.
        raise AssertionError(f'{GnoiPaths.STATE_FILE} is not valid JSON:\n{output}')


def state_file_counters(state, rpc_name):
    """
    Return (receivedCalls, failedCalls) for one RPC from the raw state-file JSON.

    The per-RPC entries sit under 'rpcs', next to the state-file schema version.
    """
    rpcs = state.get(GnoiRpcCounters.STATE_FILE_RPCS, {}) if isinstance(state, dict) else {}
    entry = rpcs.get(rpc_name, {}) if isinstance(rpcs, dict) else {}
    return (_counter_value(entry, GnoiRpcCounters.STATE_FILE_RECEIVED),
            _counter_value(entry, GnoiRpcCounters.STATE_FILE_FAILED))


def file_exists_on_dut(engine, path) -> bool:
    return 'present' in engine.run_cmd(f'sudo test -e {path} && echo present || echo absent')


def verify_gnoi_state_files_removed(engine):
    """
    Both state files are removed when nv-gnmi stops. The gNMI one matters too: NVUE uses its
    presence to detect that the gNMI server is enabled, so leaving it behind is a real bug.
    """
    for path in (GnoiPaths.STATE_FILE, GnoiPaths.GNMI_STATE_FILE):
        assert not file_exists_on_dut(engine, path), \
            f'{path} still exists after the nv-gnmi service stopped'


def get_dut_uptime_sec(engine) -> float:
    """Uptime is the cheapest way to prove an RPC did not reboot the switch."""
    return float(engine.run_cmd("cat /proc/uptime").split()[0])


def verify_dut_did_not_reboot(engine, uptime_before_sec):
    uptime_now = get_dut_uptime_sec(engine)
    assert uptime_now >= uptime_before_sec, (
        f'The switch rebooted unexpectedly: uptime dropped from {uptime_before_sec}s to {uptime_now}s')


def verify_gnoi_artifacts_in_techsupport(engine, techsupport_tar):
    """
    Assert the collected gNOI artifacts (HLD 7.5) are inside the dump and the staging area is not.

    The two artifacts HLD 7.5 asks for that the current build does not collect are logged as
    findings, and a build that starts collecting them is logged too so the check can be tightened.
    """
    listing = engine.run_cmd(f'sudo tar -tf {techsupport_tar} 2>/dev/null')
    assert listing.strip(), f'tech-support tarball is not readable: {techsupport_tar}'

    missing = [artifact for artifact in TECHSUPPORT_EXPECTED_GNOI_ARTIFACTS if artifact not in listing]
    assert not missing, (f'gNOI artifacts missing from the tech-support dump {techsupport_tar}: '
                         f'{missing}')

    for artifact in TECHSUPPORT_UNCOLLECTED_GNOI_ARTIFACTS:
        if artifact in listing:
            logger.info(f'{artifact} is now collected into the tech-support dump; it can be moved '
                        'into TECHSUPPORT_EXPECTED_GNOI_ARTIFACTS')
        else:
            logger.warning(f'HLD 7.5 finding: {artifact} is not collected into {techsupport_tar}')

    assert TECHSUPPORT_FORBIDDEN_GNOI_ARTIFACT not in listing, (
        f'the transient gNOI staging area was collected into {techsupport_tar}; it must be excluded '
        '(size + privacy)')


def parse_nvue_file_list(output):
    """
    Return the entries of 'nv action list system file-path <path>' as a list of dicts.

    The action prints its progress ('Action executing ...') with cursor-control escapes ahead of the
    JSON, and those escapes contain '[' themselves, so the array is found by the line that holds
    nothing but a bracket rather than by the first bracket in the text.
    """
    lines = (output or '').splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == '['), None)
    end = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip() == ']'), None)
    assert start is not None and end is not None and end > start, \
        f'the file-path listing carries no JSON array:\n{output}'
    try:
        return json.loads('\n'.join(lines[start:end + 1]))
    except ValueError as err:
        raise AssertionError(f'the file-path listing is not valid JSON ({err}):\n{output}')


def find_listing_entry(entries, filename):
    """Return the listed entry whose filename matches, by full path or basename."""
    for entry in entries:
        listed = str(entry.get(GnoiFileService.LIST_FILENAME_KEY, ''))
        if listed == filename or listed.rstrip('/').rsplit('/', 1)[-1] == filename.rsplit('/', 1)[-1]:
            return entry
    return None


def dut_file_attributes(engine, path):
    """
    Return (mode, owner, size) for a file on the switch.

    The switch is the authority on what a transfer actually produced, so ownership and mode are
    read here rather than from the client's view of the RPC.
    """
    output = engine.run_cmd(f"sudo stat -c '%a %U %s' {path} 2>/dev/null").strip()
    assert output and len(output.split()) == 3, f'{path} does not exist on the switch: {output!r}'
    mode, owner, size = output.split()
    return mode, owner, int(size)


def dut_file_md5(engine, path):
    output = engine.run_cmd(f'sudo md5sum {path} 2>/dev/null').strip()
    assert output, f'could not checksum {path} on the switch'
    return output.split()[0]


def local_file_md5(path):
    digest = hashlib.md5()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_refusal_status(out, err, expected_status, condition, report_only=False):
    """
    Verify a refusal carries the status code tp/gnoi_TP.md §12.2 expects.

    The code is a requirement and not a detail: a caller that cannot tell an authorization refusal
    from a missing path cannot react to either. The failure is raised from an independent step so
    that the rest of the enclosing step still runs and every deviation within it is reported
    together, rather than the first one hiding the others.

    report_only keeps a condition at a warning while the status it really returns is still unknown,
    where failing would only restate what a run on hardware has to measure first.
    """
    if expected_status in f'{out or ""}\n{err or ""}':
        return

    detail = (f'{condition} did not return {expected_status} as tp/gnoi_TP.md §12.2 requires. '
              f'gnoic reported: {(err or out).strip()}')
    if report_only:
        logger.warning(f'TP 12.2 finding: {detail}')
        return

    with allure.independent_step(f'{condition} is refused with {expected_status}'):
        raise AssertionError(detail)


def single_retrieved_file(local_dir):
    """
    Return the one file a File.Get produced under local_dir.

    gnoic rebuilds the remote directory tree below --dst, so a get of /home/admin/f.txt lands at
    <local_dir>/home/admin/f.txt rather than <local_dir>/f.txt.
    """
    retrieved = [os.path.join(root, name)
                 for root, _, names in os.walk(local_dir) for name in names]
    assert len(retrieved) == 1, f'expected exactly one retrieved file under {local_dir}: {retrieved}'
    return retrieved[0]


def verify_staging_area_clean(engine, context=''):
    """
    Assert no staged bytes survive a completed transfer.

    The staging area is a 4GB loop-mounted image, so residue both consumes a bounded resource and
    leaves transferred content readable after the RPC is over.
    """
    residue = engine.run_cmd(f'sudo find {GnoiPaths.STAGING_DIR} -type f 2>/dev/null').strip()
    assert not residue, f'files were left in the gNOI staging area {context}:\n{residue}'


def verify_rpc_counters_increased(gnoi_rpc_component, baseline, rpc_names, failed=False):
    """
    Assert the received (or failed) counter of every listed RPC moved past its baseline.

    Counters are compared as "increased" rather than by an exact delta because nv-gnoid
    double-counts some RPCs (see the COUNTER_DELTA note in test_gnoi_counters.py).
    """
    index = 1 if failed else 0
    leaf = GnoiRpcCounters.FAILED if failed else GnoiRpcCounters.RECEIVED
    current = get_gnoi_rpc_counters(gnoi_rpc_component)
    for rpc_name in rpc_names:
        before = baseline.get(rpc_name, (0, 0))[index]
        after = current.get(rpc_name, (0, 0))[index]
        assert after > before, \
            f'{rpc_name} {leaf} did not move after the RPC was served: {before} -> {after}'
        logger.info(f'{rpc_name} {leaf}: {before} -> {after}')
    return current


def verify_counters_unchanged(gnoi_rpc_component, baseline, context=''):
    """
    Assert nv-gnoid counted nothing, i.e. the request never reached it.

    Used for requests refused at the Envoy ingress: the proof that authentication happens in front
    of the server is that the server's own counters do not move.
    """
    current = get_gnoi_rpc_counters(gnoi_rpc_component)
    changed = {name: (baseline.get(name, (0, 0)), counts)
               for name, counts in current.items() if baseline.get(name, (0, 0)) != counts}
    assert not changed, f'nv-gnoid counted a request it should never have seen {context}: {changed}'
    return current


def platform_firmware_versions(platform, engine=None):
    """Return {component: actual-firmware} as `nv show platform firmware` reports it."""
    output = OutputParsingTool.parse_json_str_to_dictionary(
        platform.firmware.show(dut_engine=engine)).get_returned_value()
    return {component: fields.get(PlatformConsts.FW_ACTUAL)
            for component, fields in output.items() if isinstance(fields, dict)}


def firmware_file_for_component(component):
    """
    Return (path, filename, version) for a component's firmware file, or None when there is none.

    The lab's platform-components JSON is the source, the same one the platform firmware test uses.
    A component can be absent from it, or name a file this host cannot read, and both mean "not a
    candidate" rather than a failure.
    """
    json_key = GnoiFirmware.COMPONENT_TO_JSON_KEY.get(component)
    if not json_key:
        return None
    try:
        path, filename, version = BmcTool.get_fw_component_version_latest(json_key)
    except (KeyError, TypeError, OSError) as error:
        logger.info(f'no firmware entry for {component} ({json_key}): {error}')
        return None
    if not path or not filename or not os.path.isfile(path):
        logger.info(f'firmware file for {component} is unavailable: {path}')
        return None
    return path, filename, version


def firmware_component_candidates(platform, engine=None):
    """Components present on this switch that also have a readable firmware file."""
    on_switch = platform_firmware_versions(platform, engine)
    candidates = {}
    for component in on_switch:
        entry = firmware_file_for_component(component)
        if entry:
            candidates[component] = entry
    return candidates


def pick_firmware_component(platform, engine=None, prefer=None):
    """
    Choose a component to exercise SetPackage against, at random among the viable ones.

    Random on purpose: the per-component path is generic, so rotating the target across runs covers
    more of it than always picking the same one. The choice is logged so a failure can be repeated.
    """
    candidates = firmware_component_candidates(platform, engine)
    assert candidates, ('no platform component on this switch has a firmware file available. '
                        'Check --fw_versions_json_file and that its paths are readable here')
    component = prefer if prefer in candidates else random.choice(sorted(candidates))
    path, filename, version = candidates[component]
    logger.info(f'exercising SetPackage against {component} (of {sorted(candidates)}) with '
                f'{path}, version {version}')
    return component, path, filename, version


def component_store_dir(component):
    """
    Return the directory the firmware store keeps a component's files in.

    Named after the component family and not the component: CPLD1 through CPLD4 all share
    /host/fw-images/cpld, so simply lower-casing the component id names a directory that does not
    exist. The family is the same key the platform-components JSON is indexed by, which is why that
    map answers this, and only components appearing in it can ever be picked for a transfer.
    """
    return GnoiFirmware.COMPONENT_TO_JSON_KEY[component]


def component_firmware_files(platform, component, engine=None):
    """Names of the files currently in a component's firmware store."""
    store = PlatformComponent(platform.firmware, component_name=component)
    output = OutputParsingTool.parse_json_str_to_dictionary(
        store.files.show(dut_engine=engine)).get_returned_value()
    return list(output) if isinstance(output, dict) else []


def delete_component_firmware_file(platform, component, filename):
    """Remove one file from a component's firmware store, tolerating its absence."""
    store = PlatformComponent(platform.firmware, component_name=component)
    try:
        store.files.file_name[filename].action_delete().verify_result()
    except BaseException as error:
        logger.warning(f'could not delete {filename} from the {component} store: {error}')


def same_version_component(platform, candidates, engine=None):
    """
    Find a component whose running firmware already matches the file available for it.

    Such a component is what makes the already-installed case testable: NVUE's version check is
    expected to refuse the install, so the activate path can be driven without writing firmware.
    """
    running = platform_firmware_versions(platform, engine)
    for component, (_, _, version) in sorted(candidates.items()):
        current = running.get(component)
        if current and version and canonicalize_fw_version(current) == canonicalize_fw_version(version):
            return component
    return None


def small_local_package(suffix, size_bytes=4096):
    """
    Write a few KB carrying a given extension, for checks that are about names and not content.

    Firmware packages run to tens or hundreds of MB, and a transfer that large outlives a
    server-side rejection: the client is still writing when the stream closes and reports the
    broken write instead of the reason. A small file keeps such a check deterministic.
    """
    handle, path = tempfile.mkstemp(prefix='gnoi_fw_probe_', suffix=suffix)
    with os.fdopen(handle, 'wb') as probe:
        probe.write(b'\0' * size_bytes)
    return path


def staging_mount_line(engine):
    """Return the mount table line for the staging area, or '' when it is not mounted."""
    return engine.run_cmd(f'mount | grep {GnoiPaths.STAGING_DIR} || true').strip()


def path_mode_and_owner(engine, path):
    """Return (octal mode, owner, group) for a path, or (None, None, None) when it is absent."""
    output = engine.run_cmd(f'sudo stat -c "%a %U %G" {path} 2>/dev/null || true').strip()
    if not output:
        return None, None, None
    mode, owner, group = output.split()[:3]
    return mode, owner, group


def path_size_bytes(engine, path):
    """Return a file's size in bytes, or None when it does not exist."""
    output = engine.run_cmd(f'sudo stat -c %s {path} 2>/dev/null || true').strip()
    return int(output) if output.isdigit() else None


def path_allocated_bytes(engine, path):
    """
    Return the blocks actually allocated to a file, which for a sparse file is less than its size.

    This is what filling the staging image really costs /host. The image is sparse, so that cost
    grows as it fills, and the bound is its apparent size rather than a fixed margin.
    """
    output = engine.run_cmd(f'sudo stat -c %b {path} 2>/dev/null || true').strip()
    return int(output) * 512 if output.isdigit() else None


def process_is_running(engine, program):
    """True when at least one process matches, using a bracket class so grep misses itself."""
    output = engine.run_cmd(f'ps -ef | grep "[{program[0]}]{program[1:]}" | wc -l').strip()
    return output.isdigit() and int(output) > 0


def staging_files(engine):
    """Every regular file currently staged, as full paths."""
    output = engine.run_cmd(f'sudo find {GnoiPaths.STAGING_DIR} -type f 2>/dev/null || true').strip()
    return [line.strip() for line in output.splitlines() if line.strip()]


def wait_for_staged_file(engine, timeout=GnoiStaging.STAGED_FILE_APPEARS_TIMEOUT_SEC, poll=2):
    """
    Wait for a transfer in flight to appear in the staging area and return its path.

    Returns None on timeout, which a caller should treat as "could not observe" rather than as a
    failure: a small or fast transfer can finish before the first poll.
    """
    started = time.time()
    while time.time() - started < timeout:
        staged = staging_files(engine)
        if staged:
            return staged[0]
        time.sleep(poll)
    return None


def container_memory_bytes(engine, container=GnoiStaging.CONTAINER):
    """
    Return the container's current memory use, from its cgroup via systemd.

    MemoryCurrent is used rather than 'docker stats' because it is a single number that needs no
    parsing of human-readable units, and it covers the same cgroup.
    """
    output = engine.run_cmd(
        f'systemctl show {container} -p MemoryCurrent --value 2>/dev/null || true').strip()
    return int(output) if output.isdigit() else None


def filesystem_free_bytes(engine, path):
    """Free bytes on the filesystem holding path."""
    output = engine.run_cmd(f"df -B1 --output=avail {path} | tail -1").strip()
    return int(output) if output.isdigit() else None


def create_sparse_file(path, size_bytes):
    """
    Create a sparse file of the given size on the test host.

    Sparse on purpose: the client streams the zeros it reads, so the payload can exceed the
    switch's staging image without needing that much disk locally.
    """
    subprocess.check_call(['truncate', '-s', str(size_bytes), path])
    logger.info(f'created a {size_bytes} byte sparse payload at {path}')
    return path


def control_gnoi_service(engine, action, service=GnoiStaging.SERVICE):
    """Start or stop the gNMI/gNOI service, waiting for systemd to settle."""
    engine.run_cmd(f'sudo systemctl {action} {service}')
    time.sleep(5)
    state = gnoi_service_state(engine, service)
    logger.info(f'{service} is {state!r} after {action}')
    return state


def gnoi_service_state(engine, service=GnoiStaging.SERVICE):
    """Report systemd's view of the service, e.g. 'active' or 'inactive'."""
    return engine.run_cmd(f'systemctl is-active {service} || true').strip()


def restore_gnoi_service(engine, state, client=None, service=GnoiStaging.SERVICE):
    """
    Put the service back in the state the test found it in.

    A test that stops the service and then fails before starting it again would otherwise leave
    gNOI down for everything that runs after it on the same switch. Pass client to wait for gNOI to
    serve again: systemd calling the unit active is not the same as the RPC being answered, and
    without the wait the next test races Envoy regenerating its /gnoi route.
    """
    current = gnoi_service_state(engine, service)
    if current == state:
        return False

    action = 'start' if state == GnoiStaging.SERVICE_ACTIVE else 'stop'
    logger.warning(f'{service} is {current!r} but was {state!r} before the test - running {action}')
    control_gnoi_service(engine, action, service)
    if client is not None and state == GnoiStaging.SERVICE_ACTIVE:
        wait_for_gnoi_to_answer(client)
    return True


def wait_for_gnoi_to_answer(client, timeout=GnoiStaging.SERVICE_RESTART_TIMEOUT_SEC, poll=5):
    """
    Wait for gNOI to serve a File.Stat again after a restart or reboot.

    The service being active is not the same as the RPC being served: Envoy has to regenerate its
    /gnoi route and nv-gnoid has to come up behind it.
    """
    started = time.time()
    while time.time() - started < timeout:
        out, err = client.file_stat(GnoiPaths.STAT_PROBE_PATH)
        if not gnoi_rpc_failed(out, err):
            elapsed = time.time() - started
            logger.info(f'gNOI answered {elapsed:.0f}s after the service came back')
            return elapsed
        time.sleep(poll)
    return None


def system_health_issues(system, engine=None):
    """
    Return the component names currently reported as unhealthy.

    Compared as a before/after set rather than demanding OK outright: lab switches carry standing
    hardware alarms (juliet-128 reports a coolant-leak sensor on every boot), and a reboot test
    should notice what it broke without inheriting what was already broken.
    """
    output = OutputParsingTool.parse_json_str_to_dictionary(
        system.health.show(dut_engine=engine)).get_returned_value()
    return set(output.get(HealthConsts.ISSUES, {}))


def remote_reboot_command(topology_obj, dut_alias='dut'):
    """
    Return NOGA's remote-reboot command for the DUT, or None when the setup has none.

    NOGA's own string is preferred over building one, because it names the host token that the
    rreboot config knows - which is not always the management hostname.
    """
    for alias in (f'{dut_alias}_serial', dut_alias):
        try:
            attributes = topology_obj.players[alias]['attributes'].noga_query_data['attributes']
            command = attributes['Specific'].get('remote_reboot')
        except (KeyError, AttributeError):
            continue
        if command:
            return command
    return None


def dut_responds_to_ping(ip):
    """One short ping, used to watch a switch go down and come back."""
    return subprocess.call(['ping', '-c', '1', '-W', '2', ip],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def wait_until_dut_unreachable(ip, timeout=GnoiErrorFlows.HALT_DOWN_TIMEOUT_SEC,
                               poll=GnoiErrorFlows.HALT_PING_POLL_SEC):
    """
    Wait for the switch to stop answering pings, returning how long that took, or None on timeout.

    Framework ping helpers retry internally, which is the opposite of what is needed here: the
    question is when connectivity stops, so each attempt has to be taken at face value.
    """
    started = time.time()
    while time.time() - started < timeout:
        if not dut_responds_to_ping(ip):
            elapsed = time.time() - started
            logger.info(f'{ip} stopped answering pings after {elapsed:.0f}s')
            return elapsed
        time.sleep(poll)
    return None


def run_remote_reboot(command, timeout=GnoiErrorFlows.REMOTE_REBOOT_TIMEOUT_SEC):
    """
    Power-cycle the switch by running NOGA's remote-reboot command on the test host.

    The framework runs this over SSH to the site server, which needs TEST_SERVER_* credentials.
    Here it runs locally instead: the script lives on the shared filesystem and the PDU it talks to
    is reachable from the test host, so the detour buys nothing.
    """
    logger.info(f'recovering the switch with: {command}')
    out, err, _ = CmdRunner('GnoiRemoteReboot', timeout).run_cmd_in_process(command,
                                                                            cmd_timeout=timeout)
    logger.info(f'remote reboot output: {out}\n{err}')
    return out, err


def dut_log_position(engine, log_path):
    """
    Return the current line count of a log, to read only what an action appends.

    These logs persist across runs and the gNOI one reaches megabytes, so matching a marker
    anywhere in the file would happily match a previous run's evidence.
    """
    output = engine.run_cmd(f'sudo wc -l < {log_path} 2>/dev/null').strip()
    return int(output) if output.isdigit() else 0


def dut_log_since(engine, log_path, position, marker=None):
    """
    Return the log lines written after position, optionally only those holding marker.

    grep -a is required: the gNOI log carries bytes that make grep treat it as binary
    and skip it silently, which looks exactly like 'the switch logged nothing'.
    """
    cmd = f'sudo tail -n +{position + 1} {log_path} 2>/dev/null'
    if marker:
        cmd += f' | grep -a -F -- {shlex.quote(marker)}'
    return engine.run_cmd(cmd)


def verify_log_records(engine, log_path, position, marker, context, report_only=False):
    """
    Assert the switch logged marker while serving the action just performed.

    A refusal that leaves no audit trail is a defect in its own right, not a detail: tp/gnoi_TP.md
    §6.1 expects the switch to record what it refused, and an operator investigating a denial has
    only this record to go on.

    report_only downgrades it to a warning where the record is known to be absent by design of the
    current implementation, so that failing would restate a gap the caller has already accounted
    for rather than report a new one.
    """
    found = dut_log_since(engine, log_path, position, marker)
    if found.strip():
        logger.info(f'{log_path} recorded {context}: {found.strip().splitlines()[-1]}')
        return found

    detail = (f'{log_path} carries no {marker!r} line for {context}; the switch is '
              'expected to record it')
    if report_only:
        logger.warning(f'TP 6.1 finding: {detail}')
        return found
    raise AssertionError(detail)


def world_readable_log_file(engine):
    """
    Return a world-readable file under the denylisted log directory.

    Picked at runtime rather than hardcoded: which logs exist varies by platform and uptime, and
    the step needs one the requesting user may genuinely read, so that a refusal can only come from
    the denylist rather than from Unix permissions.
    """
    output = engine.run_cmd(
        f'find {GnoiErrorFlows.DENYLISTED_DIR} -maxdepth 1 -type f -perm -o=r 2>/dev/null | head -1'
    ).strip()
    assert output, (f'no world-readable file under {GnoiErrorFlows.DENYLISTED_DIR} to read: the '
                    'read-path step needs one')
    return output.splitlines()[0].strip()


def resolve_local_os_image(target_version=None):
    """
    Return the NOS image gnoic will transfer.

    Two sources: the GNOI_OS_IMAGE override, which wins outright, and the image the run was given
    as --target_version, which is what MARS supplies. The caller has to name one either way, so
    that what gets installed is always something the run chose rather than whatever a previous run
    happened to leave on the player.

    Missing is a setup failure rather than a product failure, so it is reported as such: the image
    is lab material the test cannot produce for itself.
    """
    override = os.environ.get(GnoiOsService.LOCAL_IMAGE_ENV_VAR)
    if override:
        assert os.path.isfile(override), \
            f'{GnoiOsService.LOCAL_IMAGE_ENV_VAR} names {override}, which is not a file'
        return override

    assert target_version and os.path.isfile(target_version), (
        f'no NOS image to install: --target_version is {target_version!r}. Name an image with '
        f'--target_version, or point {GnoiOsService.LOCAL_IMAGE_ENV_VAR} at one.')
    return target_version


def installer_version(local_image):
    """
    Return the version the ONIE installer declares in its header.

    Taking the version from the image itself rather than from a constant keeps the RPC's version
    field and the image in step when the lab build is swapped.
    """
    with open(local_image, 'rb') as handle:
        header = handle.read(GnoiOsService.INSTALLER_HEADER_BYTES).decode('latin-1')
    match = re.search(GnoiOsService.INSTALLER_VERSION_PATTERN, header)
    assert match, f'{local_image} carries no image_version in its installer header'
    return match.group(1)


def build_id_for_version(version):
    """
    Return the build-id NVOS reports for an installer version.

    The ONIE installer labels what it writes 'nvos-${image_version}', so the version passed to
    OS.Install and the build-id shown by 'nv show system image' differ by that prefix.
    """
    return version if version.startswith('nvos-') else f'nvos-{version}'


def get_image_state(system):
    """
    Return {'current', 'next', 'partition1', 'partition2'} from 'nv show system image'.

    A switch with a single installed image reports no partition2 at all, so absent partitions come
    back as empty rather than raising.
    """
    return system.image.get_image_field_values()


def partition_build_id(image_state, partition):
    """Return the build-id a partition holds, or '' when that partition does not exist."""
    entry = image_state.get(partition) or {}
    return entry.get(ImageConsts.BUILD_ID, '') if isinstance(entry, dict) else ''


def partition_holding_version(image_state, build_id):
    """Return the partition name holding build_id, or None."""
    for partition in (ImageConsts.PARTITION1_IMG, ImageConsts.PARTITION2_IMG):
        if partition_build_id(image_state, partition) == build_id:
            return partition
    return None


def _grub_selected_entry(engine, timeout=GnoiOsService.GRUBENV_READ_TIMEOUT_SEC):
    """
    Return the grub menu entry index the switch would boot, or None.

    Two details of how grub keeps this, both of which have caught this test out:

    `next_entry` is a one-shot override and takes precedence, but grub consumes it during boot, so
    after a reboot only `saved_entry` remains and that is then the answer.

    The file is rewritten wholesale by an image install, and a read landing inside that window gets
    an empty listing rather than an error - hence the retry instead of concluding there is no
    boot target.
    """
    deadline = time.time() + timeout
    listing = ''
    while time.time() < deadline:
        listing = engine.run_cmd(f'sudo grub-editenv {GnoiOsService.GRUBENV} list') or ''
        for pattern in (GnoiOsService.GRUBENV_NEXT_PATTERN, GnoiOsService.GRUBENV_SAVED_PATTERN):
            match = re.search(pattern, listing)
            if match:
                return int(match.group(1))
        logger.info('the grub environment names no entry, which an image install causes while it '
                    'rewrites the file; re-reading')
        time.sleep(2)
    logger.warning(f'grub named no boot entry within {timeout}s. Last listing: {listing!r}')
    return None


def effective_boot_next_partition(engine, system):
    """
    Return the partition the switch will actually boot next, read from grub rather than from NVUE.

    `nv action boot-next` rewrites grub's saved entry immediately, while the `next` field of
    `nv show system image` lags behind it by around a minute. Grub is therefore the only place to
    ask what the switch would do if it rebooted right now.

    Resolved through the build-id rather than by assuming grub's entries are ordered to match the
    partition numbering: the selected menu entry names an image, and the per-partition build-ids
    say which partition holds it.

    Those build-ids are read from `nv show system image` and not from
    /host/image-partition-slot-map.json, which an install leaves a generation behind: it still named
    the image the install had just overwritten, so the build-id grub pointed at belonged to no
    partition at all and this returned None for a switch that was set up correctly.
    """
    entry_index = _grub_selected_entry(engine)
    if entry_index is None:
        return None

    menu = engine.run_cmd(f'sudo grep -a "^menuentry" {GnoiOsService.GRUB_CFG}')
    titles = re.findall(GnoiOsService.GRUB_MENUENTRY_PATTERN, menu or '', flags=re.MULTILINE)
    if entry_index >= len(titles):
        logger.warning(f'grub points at entry {entry_index} but only {len(titles)} exist: {titles}')
        return None
    selected_image = titles[entry_index]

    image_state = get_image_state(system)
    partition = partition_holding_version(image_state, selected_image)
    if partition is None:
        logger.warning(f'grub boots {selected_image!r}, which no partition holds: {image_state}')
    return partition


def wait_for_reported_boot_next(system, expected_partition,
                                timeout=GnoiOsService.BOOT_NEXT_VISIBILITY_TIMEOUT_SEC):
    """
    Wait for `nv show system image` to report the boot-next that grub already holds.

    Returns the seconds it took, or None if it never agreed. The delay is the point of interest:
    an operator who sets boot-next and reads it back sees stale state for about a minute.
    """
    expected = expected_partition.replace(ImageConsts.PARTITION, '')
    started = time.time()
    while time.time() - started < timeout:
        state = system.image.get_image_field_values()
        if str(state.get(ImageConsts.NEXT_IMG)) == expected:
            return time.time() - started
        time.sleep(5)
    return None


def images_on_dut(engine):
    """Return the file names sitting in the switch's NOS image directory."""
    output = engine.run_cmd(f'sudo ls -1 {GnoiOsService.IMAGE_DIR} 2>/dev/null').strip()
    return [line.strip() for line in output.splitlines() if line.strip()]


def verify_install_response(out, err, version):
    """
    Assert the InstallResponse sequence ended in Validated for the requested version.

    Reads the prototext gnoic emits under --print-proto: its log lines report the transfer but
    never the responses that decide the outcome, so success is only visible here.

    A target that already holds the version is allowed to answer Validated without transferring
    anything, so the transfer is reported rather than demanded; the caller decides what that means
    for the steps that follow.
    """
    merged = f'{out or ""}\n{err or ""}'
    assert GnoiOsService.INSTALL_ERROR not in merged, f'OS.Install reported an install error:\n{merged}'
    assert not gnoi_rpc_failed(out, err), f'OS.Install failed.\nout: {out}\nerr: {err}'

    validated = re.search(GnoiOsService.VALIDATED + r':\s*{[^}]*version:\s*"([^"]+)"', merged)
    assert validated, (f'OS.Install did not end in a {GnoiOsService.VALIDATED} response. '
                       f'Last of the output:\n{merged[-2000:]}')
    assert validated.group(1) == version, (
        f'OS.Install validated {validated.group(1)!r}, but {version!r} was requested')

    transferred = GnoiOsService.TRANSFER_READY in merged
    if transferred:
        logger.info(f'OS.Install transferred the image: {GnoiOsService.TRANSFER_READY} then '
                    f'{merged.count(GnoiOsService.TRANSFER_PROGRESS)} progress responses, '
                    f'validated as {validated.group(1)}')
    else:
        logger.info(f'OS.Install answered {GnoiOsService.VALIDATED} without a transfer: the switch '
                    'already holds this version')
    return transferred


def verify_gnoi_reachable(client, rpc_probe_path=GnoiPaths.STAT_PROBE_PATH):
    """Prove gNOI answers, using File.Stat as the non-destructive probe."""
    with allure.step('verify gNOI is served (File.Stat probe)'):
        out, err = client.file_stat(rpc_probe_path)
        assert not gnoi_is_not_served(out, err), (
            f'gNOI did not answer on {client.server_host}:{client.server_port}\nout: {out}\nerr: {err}')
        return out, err


def verify_gnoi_not_served(client, rpc_probe_path=GnoiPaths.STAT_PROBE_PATH, cmd_time=None):
    """Prove the endpoint does not answer gNOI at all (gNMI server disabled)."""
    with allure.step('verify gNOI is not served'):
        out, err = client.file_stat(rpc_probe_path, cmd_time=cmd_time)
        assert gnoi_is_not_served(out, err), (
            'gNOI answered while the gNMI server is disabled; it must not be served.\n'
            f'out: {out}\nerr: {err}')
        return out, err
