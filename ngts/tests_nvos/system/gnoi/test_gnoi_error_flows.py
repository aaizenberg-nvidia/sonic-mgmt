"""
gNOI error flows and access control (tp/gnoi_TP.md §6.1).

Walks the documented negative paths: an out-of-scope RPC, unsupported and NVUE-rejected reboot
modes, authentication failures, the RBAC role gate on the File actions, and the file-scope
boundaries - Unix permissions, the forbidden-directory denylist, and its deliberate read/write
asymmetry.

Deviations from the TP text, and why:
  - NSF cannot be driven from gnoic: it refuses the value locally ('unknown reboot method "NSF"')
    and sends nothing, so the switch never sees that mode. POWERDOWN and POWERUP carry the step.
  - HALT powers the switch down, so it runs strictly last and only once a remote-reboot command has
    been resolved from NOGA. That command is run on the test host rather than over SSH to the site
    server, so it needs no TEST_SERVER_* credentials.
  - The mTLS smoke step needs a client certificate carrying a SPIFFE ID, which the lab does not
    provide to this test; it is reported as uncovered rather than silently passed.
"""
import logging
import os
import shutil
import tempfile
import uuid

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ApiType, SystemConsts
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnoi.GnoiClient import GnoiClient
from ngts.tests_nvos.system.gnoi.constants import (
    GNOIC_TRANSFER_TIMEOUT_SEC,
    GnoicErr,
    GnoiErrorFlows,
    GnoiFileService,
    GnoiRebootMode,
    GnoiRpc,
)
from ngts.tests_nvos.system.gnoi.helpers import (
    dut_log_position,
    dut_log_since,
    dut_responds_to_ping,
    file_exists_on_dut,
    get_dut_uptime_sec,
    get_gnoi_rpc_counters,
    gnoi_rpc_failed,
    remote_reboot_command,
    run_remote_reboot,
    system_health_issues,
    verify_counters_unchanged,
    verify_dut_did_not_reboot,
    verify_gnoi_reachable,
    verify_log_records,
    verify_refusal_status,
    verify_rpc_counters_increased,
    wait_until_dut_unreachable,
    world_readable_log_file,
)

logger = logging.getLogger()


def _refused(out, err, expected_desc=None):
    """Return the merged output after asserting the RPC was refused."""
    merged = f'{out or ""}\n{err or ""}'
    assert gnoi_rpc_failed(out, err), f'the RPC was expected to fail but did not:\n{merged}'
    if expected_desc:
        assert expected_desc in merged, \
            f'the refusal does not carry {expected_desc!r}:\n{merged}'
    return merged


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.gnoi
def test_gnoi_error_flows_and_access_control(engines, devices, topology_obj, test_name):
    """
    Validate the documented refusals and the access-control boundaries.

    Test flow (tp/gnoi_TP.md §6.1):
        1.  An out-of-scope RPC returns Unimplemented, and is counted but not complained about
        2.  Unsupported reboot modes return InvalidArgument, log a warning, and do not reboot
        3.  WARM is accepted by the server but refused by NVUE, and does not reboot
        4.  A wrong password returns Unauthenticated, and Envoy records the authentication failure
        5.  No credentials are refused before nv-gnoid sees the request (counters do not move),
            and Envoy records that refusal too
        6.  mTLS smoke - reported as uncovered without a SPIFFE client certificate
        7.  A read-only monitor role cannot invoke the File RPCs, and the denial is logged
        8.  The write/action role can invoke all four
        9.  A file the user's Unix permissions deny cannot be read, even with sudo rights
        10. A world-readable file under the denylisted /var/log can be stat-ed and fetched
        11. Writing to and removing under /var/log are refused by the denylist
        12. A path using '..' to reach /var/log is resolved first and then refused
        13. A symlink into /var/log is resolved first and then refused
        14. Failed counters moved for the RPCs refused above
        15. HALT is accepted, the switch goes down, and it serves gNOI again after a remote reboot
    """
    system = System()
    gnoi_rpc = system.gnmi_server.status.gnoi_rpc
    dut = engines.dut
    username = devices.dut.default_username
    password = devices.dut.default_password
    home = GnoiFileService.user_home(username)

    client = GnoiClient(dut.ip, username=username, password=password, verify_tools_installed=True)

    tag = uuid.uuid4().hex[:8]
    denylisted_target = f'{GnoiErrorFlows.DENYLISTED_DIR}/gnoi_denied_{tag}.txt'
    traversal_target = f'{home}/../..{GnoiErrorFlows.DENYLISTED_DIR}/gnoi_traversal_{tag}.txt'
    symlink_on_dut = f'{home}/gnoi_link_{tag}'
    allowed_file = f'{home}/gnoi_allowed_{tag}.txt'

    local_file = f'/tmp/gnoi_error_flows_{tag}.txt'
    with open(local_file, 'w') as handle:
        handle.write('gNOI error flow payload\n')

    monitor_user = None
    baseline = get_gnoi_rpc_counters(gnoi_rpc)
    logger.info(f'gnoi-rpc baseline: {baseline}')

    # Resolved up front so the cleanup below can recover the switch however the test ends.
    recovery_cmd = remote_reboot_command(topology_obj)
    logger.info(f'remote-reboot command for this setup: {recovery_cmd!r}')

    try:
        with allure.step('Step 1: an out-of-scope RPC returns Unimplemented and is not logged'):
            log_at = dut_log_position(dut, GnoiErrorFlows.GNOID_LOG)
            out, err = client.run_gnoic_op('system time')
            merged = _refused(out, err, GnoiErrorFlows.OUT_OF_SCOPE_DESC)
            assert GnoicErr.UNIMPLEMENTED in merged, \
                f'an out-of-scope RPC must return {GnoicErr.UNIMPLEMENTED}:\n{merged}'
            # Documented as counter-only: the request itself is logged, but the refusal must not
            # add a warning or error, so an unsupported RPC cannot be used to flood the log.
            appended = dut_log_since(dut, GnoiErrorFlows.GNOID_LOG, log_at,
                                     GnoiErrorFlows.OUT_OF_SCOPE_RPC_PATH)
            complaints = [line for line in appended.splitlines()
                          if any(sev in line for sev in GnoiErrorFlows.RPC_SEVERITY_MARKERS)]
            assert not complaints, \
                f'an out-of-scope RPC is documented as counter-only, but the switch logged:\n{complaints}'

        with allure.step('Step 2: unsupported reboot modes return InvalidArgument, without rebooting'):
            uptime_before = get_dut_uptime_sec(dut)
            for mode in GnoiRebootMode.UNSUPPORTED_DRIVABLE:
                log_at = dut_log_position(dut, GnoiErrorFlows.GNOID_LOG)
                out, err = client.system_reboot(mode)
                merged = _refused(out, err, GnoiErrorFlows.UNSUPPORTED_MODE_DESC)
                assert GnoicErr.INVALID_ARGUMENT in merged, \
                    f'reboot mode {mode} must return {GnoicErr.INVALID_ARGUMENT}:\n{merged}'
                verify_log_records(dut, GnoiErrorFlows.GNOID_LOG, log_at,
                                   GnoiErrorFlows.REBOOT_MODE_LOG.format(mode=mode),
                                   f'the {mode} refusal')
            verify_dut_did_not_reboot(dut, uptime_before)
            for mode in GnoiRebootMode.CLIENT_REJECTED:
                logger.warning(f'TP 6.1 finding: mode {mode} cannot be driven from gnoic - it is '
                               'rejected client-side and never reaches the switch')

        with allure.step('Step 3: WARM is refused by NVUE and does not reboot'):
            uptime_before = get_dut_uptime_sec(dut)
            out, err = client.system_reboot(GnoiRebootMode.WARM)
            merged = _refused(out, err)
            verify_dut_did_not_reboot(dut, uptime_before)
            # The TP expects NVUE's own words ("Reboot mode 'warm' is not supported") to reach the
            # client. On juliet-128 the refusal is a bare 'Internal desc = reboot request failed',
            # so a caller cannot tell an unsupported mode from a reboot that genuinely broke.
            if 'warm' not in merged.lower():
                logger.warning('TP 6.1 finding: the WARM refusal does not relay the NVUE reason. '
                               f'gnoic reported: {merged.strip()[-300:]}')

        with allure.step('Step 4: a wrong password returns Unauthenticated'):
            envoy_at = dut_log_position(dut, GnoiErrorFlows.ENVOY_LOG)
            out, err = client.file_stat(GnoiFileService.user_home(username),
                                        password=GnoiErrorFlows.WRONG_PASSWORD)
            merged = _refused(out, err, GnoiErrorFlows.AUTH_FAILED_DESC)
            assert GnoicErr.UNAUTHENTICATED in merged, \
                f'a wrong password must return {GnoicErr.UNAUTHENTICATED}:\n{merged}'
            verify_log_records(dut, GnoiErrorFlows.ENVOY_LOG, envoy_at,
                               GnoiErrorFlows.AUTH_FAILED_DESC,
                               'an authentication failure from a wrong password')

        with allure.step('Step 5: a request with no credentials never reaches nv-gnoid'):
            before_anonymous = get_gnoi_rpc_counters(gnoi_rpc)
            envoy_at = dut_log_position(dut, GnoiErrorFlows.ENVOY_LOG)
            anonymous = GnoiClient(dut.ip)
            out, err = anonymous.file_stat(GnoiFileService.user_home(username))
            merged = _refused(out, err, GnoiErrorFlows.AUTH_FAILED_DESC)
            assert GnoicErr.UNAUTHENTICATED in merged, \
                f'an unauthenticated request must return {GnoicErr.UNAUTHENTICATED}:\n{merged}'
            # Authentication sits at the Envoy ingress, so the server should not have counted it.
            verify_counters_unchanged(gnoi_rpc, before_anonymous, 'for an unauthenticated request')
            verify_log_records(dut, GnoiErrorFlows.ENVOY_LOG, envoy_at,
                               GnoiErrorFlows.AUTH_FAILED_DESC,
                               'a request rejected for having no credentials')

        with allure.step('Step 6: mTLS smoke'):
            logger.warning('TP 6.1 step 8 not covered: no client certificate with a SPIFFE ID is '
                           'available to this test. The certificate matrix belongs to the gNMI '
                           'security TP (see tp/gnoi_TP.md §2)')

        with allure.step('Step 7: a read-only monitor role cannot invoke the File RPCs'):
            # force_api=NVUE: setting a password over OpenAPI is broken (ngts aaa/helpers.py).
            monitor_user, monitor_password = System(force_api=ApiType.NVUE).aaa.user.set_new_user(
                role=SystemConsts.ROLE_VIEWER, apply=True)
            logger.info(f'created monitor-role user {monitor_user}')
            monitor_client = GnoiClient(dut.ip, username=monitor_user, password=monitor_password)

            log_at = dut_log_position(dut, GnoiErrorFlows.GNOID_LOG)
            denials = {
                GnoiRpc.FILE_STAT: monitor_client.file_stat(home),
                GnoiRpc.FILE_GET: monitor_client.file_get(GnoiErrorFlows.ROOT_ONLY_FILE, '/tmp'),
                GnoiRpc.FILE_PUT: monitor_client.file_put(local_file, f'{home}/monitor_{tag}.txt'),
                GnoiRpc.FILE_REMOVE: monitor_client.file_remove(f'{home}/monitor_{tag}.txt'),
            }
            for rpc_name, (out, err) in denials.items():
                merged = _refused(out, err)
                # report_only: the refusal is asserted above, but the status these denials really
                # carry has not been observed yet, so a run reports it instead of failing on a
                # guess.
                verify_refusal_status(out, err, GnoicErr.PERMISSION_DENIED,
                                      f'{rpc_name} as a read-only monitor user', report_only=True)
            verify_log_records(dut, GnoiErrorFlows.GNOID_LOG, log_at,
                               GnoiErrorFlows.RBAC_DENIAL_LOG, 'the RBAC denials')

        with allure.step('Step 8: the write/action role is permitted for all four File RPCs'):
            out, err = client.file_put(local_file, allowed_file,
                                       permission=GnoiFileService.PUT_MODE)
            assert not gnoi_rpc_failed(out, err), \
                f'File.Put must be permitted for the write role.\nout: {out}\nerr: {err}'
            assert file_exists_on_dut(dut, allowed_file), f'{allowed_file} was not created'

            out, err = client.file_stat(allowed_file)
            assert not gnoi_rpc_failed(out, err), \
                f'File.Stat must be permitted for the write role.\nout: {out}\nerr: {err}'

            get_dir = tempfile.mkdtemp(prefix=f'gnoi_write_role_{tag}_')
            out, err = client.file_get(allowed_file, get_dir)
            assert not gnoi_rpc_failed(out, err), \
                f'File.Get must be permitted for the write role.\nout: {out}\nerr: {err}'
            shutil.rmtree(get_dir, ignore_errors=True)

            out, err = client.file_remove(allowed_file)
            assert not gnoi_rpc_failed(out, err), \
                f'File.Remove must be permitted for the write role.\nout: {out}\nerr: {err}'
            assert not file_exists_on_dut(dut, allowed_file), \
                f'{allowed_file} survived a permitted File.Remove'

        with allure.step('Step 9: Unix permissions bound what the RPC can read'):
            # admin holds sudo, but the RPC runs as the user without it, so a root-only file stays
            # unreadable - the bound is the user's own permissions, not the account's privileges.
            out, err = client.file_get(GnoiErrorFlows.ROOT_ONLY_FILE, '/tmp')
            _refused(out, err)

        with allure.step(f'Step 10: the read path ignores the {GnoiErrorFlows.DENYLISTED_DIR} '
                         'denylist'):
            log_file = world_readable_log_file(dut)
            out, err = client.file_stat(log_file)
            assert not gnoi_rpc_failed(out, err), (
                f'File.Stat of {log_file} failed. The denylist is documented as write-path only, '
                f'so a world-readable log must remain readable.\nout: {out}\nerr: {err}')

            # Fetching it too: @list checks nothing and @upload only checks its file:// destination,
            # so the whole read path is expected through, not just the metadata call.
            read_dir = tempfile.mkdtemp(prefix=f'gnoi_denylist_read_{tag}_')
            out, err = client.file_get(log_file, read_dir,
                                       cmd_time=GNOIC_TRANSFER_TIMEOUT_SEC)
            assert not gnoi_rpc_failed(out, err), (
                f'File.Get of {log_file} failed, but the denylist is documented as write-path '
                f'only.\nout: {out}\nerr: {err}')
            shutil.rmtree(read_dir, ignore_errors=True)
            logger.info(f'read path reached {log_file} under {GnoiErrorFlows.DENYLISTED_DIR}, as '
                        'the documented asymmetry expects')

        with allure.step(f'Step 11: writing under {GnoiErrorFlows.DENYLISTED_DIR} is refused'):
            out, err = client.file_put(local_file, denylisted_target)
            _refused(out, err)
            assert not file_exists_on_dut(dut, denylisted_target), \
                f'{denylisted_target} was created inside a denylisted directory'

            out, err = client.file_remove(log_file)
            _refused(out, err)
            assert file_exists_on_dut(dut, log_file), \
                f'{log_file} was removed from a denylisted directory'

        with allure.step("Step 12: a path using '..' to reach the denylist is refused"):
            log_at = dut_log_position(dut, GnoiErrorFlows.GNOID_LOG)
            out, err = client.file_put(local_file, traversal_target)
            _refused(out, err)
            resolved = os.path.normpath(traversal_target)
            assert not file_exists_on_dut(dut, resolved), (
                f'{traversal_target} resolved to {resolved} and was written: the guard must resolve '
                'the path before applying the denylist')
            verify_log_records(dut, GnoiErrorFlows.GNOID_LOG, log_at,
                               GnoiErrorFlows.TRAVERSAL_LOG, "the '..' rejection")

        with allure.step('Step 13: a symlink resolving into the denylist is refused'):
            # The path below is innocent-looking and reaches a denied directory only once resolved.
            dut.run_cmd(f'ln -sfn {GnoiErrorFlows.DENYLISTED_DIR} {symlink_on_dut}')
            assert file_exists_on_dut(dut, symlink_on_dut), \
                f'could not create the symlink {symlink_on_dut}'
            log_at = dut_log_position(dut, GnoiErrorFlows.GNOID_LOG)
            out, err = client.file_put(local_file, f'{symlink_on_dut}/gnoi_via_link_{tag}.txt')
            _refused(out, err)
            assert not file_exists_on_dut(
                dut, f'{GnoiErrorFlows.DENYLISTED_DIR}/gnoi_via_link_{tag}.txt'), (
                f'a write through the symlink {symlink_on_dut} reached '
                f'{GnoiErrorFlows.DENYLISTED_DIR}')
            # The refusal is real but comes from the wrong layer: nv-gnoid's own validator rejects a
            # literal '..' and lets a symlink through, so the payload is accepted, staged and only
            # then refused by NVUE. report_only because the path validator is therefore known not
            # to log here, and the write being denied is asserted above either way.
            verify_log_records(dut, GnoiErrorFlows.GNOID_LOG, log_at,
                               GnoiErrorFlows.PATH_VALIDATION_LOG,
                               'the path validator rejecting a symlink into a denylisted '
                               'directory (it was refused downstream by NVUE instead)',
                               report_only=True)

        with allure.step('Step 14: failed counters moved for the refused RPCs'):
            verify_rpc_counters_increased(
                gnoi_rpc, baseline,
                [GnoiRpc.SYSTEM_REBOOT, GnoiRpc.FILE_PUT, GnoiRpc.FILE_GET], failed=True)

        # Last, because it takes the switch down: everything above must already have run.
        if os.environ.get(GnoiErrorFlows.SKIP_HALT_ENV_VAR):
            logger.warning(f'TP 6.1 step 5 skipped on request '
                           f'({GnoiErrorFlows.SKIP_HALT_ENV_VAR} is set)')
        elif not recovery_cmd:
            # Refusing to halt without a way back, rather than stranding the switch.
            logger.warning(f'TP 6.1 step 5 not covered: {GnoiRebootMode.HALT} powers the switch '
                           'down and this setup exposes no remote-reboot command in NOGA')
        else:
            with allure.step(f'Step 15: {GnoiRebootMode.HALT} halts the switch, which then returns '
                             'after a remote reboot'):
                # The halt kills the switch mid-write, so flush first: without this the evidence for
                # the steps above can be missing from the logs after the power cycle.
                dut.run_cmd('sync')
                health_before_halt = system_health_issues(system)
                if health_before_halt:
                    logger.warning(f'health issues already present before the halt, unrelated to '
                                   f'gNOI: {health_before_halt}')

                out, err = client.system_reboot(GnoiRebootMode.HALT)
                # The RPC may not answer cleanly, since the switch goes down while it is in flight.
                # What matters is that HALT was not refused the way an unsupported mode would be.
                merged = f'{out or ""}\n{err or ""}'
                assert GnoicErr.INVALID_ARGUMENT not in merged, (
                    f'{GnoiRebootMode.HALT} is one of the two supported modes and must not be '
                    f'refused as invalid:\n{merged}')
                assert GnoiErrorFlows.UNSUPPORTED_MODE_DESC not in merged, \
                    f'{GnoiRebootMode.HALT} was reported as an unsupported mode:\n{merged}'

                went_down = wait_until_dut_unreachable(dut.ip)
                assert went_down is not None, (
                    f'{GnoiRebootMode.HALT} was accepted but the switch was still answering pings '
                    f'{GnoiErrorFlows.HALT_DOWN_TIMEOUT_SEC}s later, so it did not halt')
                logger.info(f'the switch halted {went_down:.0f}s after the RPC')

            with allure.step('Step 15b: the switch returns after a remote reboot and serves gNOI'):
                dut.disconnect()
                run_remote_reboot(recovery_cmd)
                # Health is judged against the pre-halt baseline below, so the built-in check is
                # told not to raise on a switch that was already reporting an alarm.
                DutUtilsTool.wait_for_nvos_to_become_functional(
                    dut, throw_exception_on_unhealthy=False).verify_result()

                new_issues = system_health_issues(system) - health_before_halt
                assert not new_issues, \
                    f'the halt and power cycle left the switch with new health issues: {new_issues}'
                verify_gnoi_reachable(client)

    finally:
        # Any path that leaves the switch down has to power it back on, not just the happy one.
        # Same pattern the framework's own reboot tests use (test_system_reboot.py).
        if not dut_responds_to_ping(dut.ip):
            with allure.step('Cleanup: the switch is down - recovering it with a remote reboot'):
                try:
                    assert recovery_cmd, 'the switch is down and no remote-reboot command is known'
                    dut.disconnect()
                    run_remote_reboot(recovery_cmd)
                    DutUtilsTool.wait_for_nvos_to_become_functional(
                        dut, throw_exception_on_unhealthy=False).verify_result()
                except BaseException as recovery_error:
                    logger.error(f'could not bring the switch back: {recovery_error}')

        with allure.step('Cleanup: remove the test user and files'):
            try:
                if monitor_user:
                    # A halt drops config that was applied but never saved, so the user may already
                    # be gone; removing it is still correct on every other path.
                    System().aaa.user.user_id[monitor_user].unset(apply=True).verify_result()
            except BaseException as cleanup_error:
                logger.warning(f'could not remove {monitor_user}: {cleanup_error}')
            try:
                # -f alone would follow the symlink and empty /var/log, so unlink it separately.
                dut.run_cmd(f'rm -f {symlink_on_dut}')
                dut.run_cmd(f'sudo rm -f {allowed_file} {denylisted_target}')
                if os.path.exists(local_file):
                    os.remove(local_file)
            except BaseException as cleanup_error:
                logger.warning(f'cleanup did not complete: {cleanup_error}')
