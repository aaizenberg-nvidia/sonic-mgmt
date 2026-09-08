"""
gNOI staging bounds and service recovery (tp/gnoi_TP.md §7.1).

Covers the parts of the feature that live on the host rather than on the wire: the fixed-size
disk-backed staging image, the per-user staging subdirectories, the install lock, and the fact that
none of it persists across a service restart.

The design claim under test is that a transfer costs disk and not memory, and that a runaway
transfer hits the staging image's own bound instead of filling /host. Both are checked by watching
a transfer in flight, which is why this test backgrounds its RPCs instead of running them to
completion.

Two steps are deliberately combined: one install is started in the background and, while it is in
flight, both a competing install (refused by the lock) and a file put (permitted, the documented
partial-enforcement gap of HLD §8.1) are issued against it. Driving them from separate installs
would cost another multi-GB transfer and prove nothing more.
"""
import logging
import os
import uuid

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, RebootParams
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnoi.GnoiClient import GnoiClient
from ngts.tests_nvos.system.gnoi.constants import (
    GNOIC_TRANSFER_TIMEOUT_SEC,
    GnoiFileService,
    GnoiOsService,
    GnoiPaths,
    GnoiRpc,
    GnoiStaging,
)
from ngts.tests_nvos.system.gnoi.helpers import (
    container_memory_bytes,
    create_sparse_file,
    control_gnoi_service,
    dut_log_position,
    file_exists_on_dut,
    filesystem_free_bytes,
    get_gnoi_rpc_counters,
    gnoi_rpc_failed,
    gnoi_service_state,
    installer_version,
    path_allocated_bytes,
    path_mode_and_owner,
    path_size_bytes,
    process_is_running,
    resolve_local_os_image,
    restore_gnoi_service,
    staging_files,
    staging_mount_line,
    system_health_issues,
    verify_install_response,
    verify_log_records,
    verify_rpc_counters_increased,
    verify_staging_area_clean,
    wait_for_gnoi_to_answer,
    wait_for_staged_file,
)

logger = logging.getLogger()


def _verify_gnoi_survives_reboot(system, dut, client, topology_obj, mode, health_before):
    """
    Reboot in the given mode and prove gNOI returns with a freshly recreated staging area.

    Health is judged as a delta rather than demanded outright, and the built-in check is told not
    to raise: juliet-128 carries a standing coolant-leak alarm that has nothing to do with gNOI.
    """
    # The readiness wait is done here rather than inside the reboot: the framework's own post-boot
    # check goes through the device hook, which does not carry throw_exception_on_unhealthy, so a
    # switch with a standing alarm fails the reboot itself instead of reaching the delta below.
    reboot_params = RebootParams(topology_obj=topology_obj, check_system_is_functional=False)
    system.action_reboot(additional_params={'mode': mode},
                         reboot_params=reboot_params).verify_result()
    DutUtilsTool.wait_for_nvos_to_become_functional(
        dut, throw_exception_on_unhealthy=False).verify_result()

    answered = wait_for_gnoi_to_answer(client)
    assert answered is not None, (
        f'gNOI did not serve a File.Stat after a {mode} reboot: the Envoy /gnoi route or nv-gnoid '
        'did not come back')

    mount_line = staging_mount_line(dut)
    assert GnoiPaths.STAGING_IMAGE in mount_line, \
        f'the staging area was not remounted after a {mode} reboot: {mount_line!r}'
    assert path_size_bytes(dut, GnoiPaths.STAGING_IMAGE) == GnoiStaging.IMAGE_SIZE_BYTES, \
        f'the staging image recreated by a {mode} reboot is not the documented size'
    verify_staging_area_clean(dut, f'after a {mode} reboot')
    assert process_is_running(dut, GnoiPaths.USER_LOOKUP_DAEMON), \
        f'{GnoiPaths.USER_LOOKUP_DAEMON} was not restarted after a {mode} reboot'

    # The mount is created by the control script on every start, so persisting it would be a second
    # source of truth that could outlive the service.
    fstab_entry = dut.run_cmd(f'grep -i gnoi {GnoiStaging.FSTAB} || true').strip()
    assert not fstab_entry, \
        f'the staging mount was persisted into {GnoiStaging.FSTAB}: {fstab_entry!r}'

    new_issues = system_health_issues(system) - health_before
    assert not new_issues, f'the {mode} reboot left the switch with new health issues: {new_issues}'


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.gnoi
def test_gnoi_staging_bounds_and_service_recovery(engines, devices, topology_obj, test_name,
                                                  target_version):
    """
    Validate the staging area's bounds and the service lifecycle.

    Test flow (tp/gnoi_TP.md §7.1, with the reboot half from §9):
        1. The staging image is mounted at the documented size, and the lookup socket is root-only
        2. A transfer in flight is staged under a per-user directory, owned by the requester
        3. That transfer costs disk rather than memory
        4. A payload larger than the staging image fails with ENOSPC, contained inside the image
        5. /host free space is unaffected by that failure, and staging is left clean
        6. A second OS.Install competing with one in flight is refused by the install lock
        7. A File.Put during an install is permitted - the documented partial-enforcement gap
        8. Stopping the service unmounts staging, deletes the image, and removes the state files
        9. Starting it recreates an empty staging image and gNOI serves again
        10. gNOI returns after an unclean reboot, with staging rebuilt and nothing in fstab
    """
    system = System()
    dut = engines.dut
    gnoi_rpc = system.gnmi_server.status.gnoi_rpc
    username = devices.dut.default_username
    home = GnoiFileService.user_home(username)

    client = GnoiClient(dut.ip,
                        username=username,
                        password=devices.dut.default_password,
                        verify_tools_installed=True)
    # A second client so the concurrent RPCs do not share a command runner with the transfer they
    # are competing against.
    concurrent_client = GnoiClient(dut.ip, username=username,
                                   password=devices.dut.default_password)

    tag = uuid.uuid4().hex[:8]
    local_image = resolve_local_os_image(target_version)
    version = installer_version(local_image)
    stored_image = GnoiOsService.STORED_NAME_TEMPLATE.format(version=version)
    oversized_payload = f'/var/tmp/gnoi_oversized_{tag}.bin'
    small_payload = f'/tmp/gnoi_small_{tag}.txt'
    transferred_file = f'{home}/gnoi_staged_{tag}.bin'
    during_install_file = f'{home}/gnoi_during_install_{tag}.txt'

    with open(small_payload, 'w') as handle:
        handle.write('payload sent while an install is in flight\n')

    baseline_counters = get_gnoi_rpc_counters(gnoi_rpc)
    # Steps 8 and 9 stop the service and start it again. Recorded before anything runs so that a
    # failure between the two - or anywhere after them - cannot leave gNOI down for the next test.
    service_state_before = gnoi_service_state(dut)

    try:
        with allure.step('Step 1: the staging image is mounted and the lookup socket is root-only'):
            mount_line = staging_mount_line(dut)
            assert GnoiPaths.STAGING_IMAGE in mount_line and GnoiStaging.FS_TYPE in mount_line, (
                f'{GnoiPaths.STAGING_DIR} is not a {GnoiStaging.FS_TYPE} mount of '
                f'{GnoiPaths.STAGING_IMAGE}: {mount_line!r}')

            image_size = path_size_bytes(dut, GnoiPaths.STAGING_IMAGE)
            assert image_size == GnoiStaging.IMAGE_SIZE_BYTES, (
                f'the staging image is {image_size} bytes, not the documented '
                f'{GnoiStaging.IMAGE_SIZE_BYTES}')

            socket_mode, socket_owner, _ = path_mode_and_owner(dut, GnoiPaths.USER_LOOKUP_SOCKET)
            assert socket_mode == GnoiStaging.EXPECTED_SOCKET_MODE, (
                f'{GnoiPaths.USER_LOOKUP_SOCKET} is mode {socket_mode}, not '
                f'{GnoiStaging.EXPECTED_SOCKET_MODE}: any user could ask the daemon to resolve '
                'identities')
            assert socket_owner == GnoiStaging.ROOT_OWNER, \
                f'{GnoiPaths.USER_LOOKUP_SOCKET} is owned by {socket_owner}, not root'

            dir_mode, dir_owner, _ = path_mode_and_owner(dut, GnoiStaging.SOCKET_DIR)
            if dir_mode != GnoiStaging.EXPECTED_SOCKET_DIR_MODE:
                # The socket's own 0600 still bounds access, so this is defence in depth that was
                # not delivered rather than an open door.
                logger.warning(f'TP 7.1 finding: {GnoiStaging.SOCKET_DIR} is mode {dir_mode}, '
                               f'where the HLD calls for {GnoiStaging.EXPECTED_SOCKET_DIR_MODE}. '
                               f'Owner is {dir_owner}')

            assert process_is_running(dut, GnoiPaths.USER_LOOKUP_DAEMON), \
                f'{GnoiPaths.USER_LOOKUP_DAEMON} is not running, so chown of staged files cannot work'

        with allure.step('Step 2: a transfer in flight is staged per-user and owned by the requester'):
            memory_before = container_memory_bytes(dut)
            put_process = client.start_file_put(local_image, transferred_file)
            staged_path = wait_for_staged_file(dut)
            memory_during = container_memory_bytes(dut)

            if staged_path:
                logger.info(f'observed staged file {staged_path}')
                assert f'/{username}/' in staged_path, (
                    f'{staged_path} is not under a per-user staging directory for {username}')
                _, staged_owner, _ = path_mode_and_owner(dut, staged_path)
                assert staged_owner == username, \
                    f'the staged file is owned by {staged_owner}, not the requesting {username}'
            else:
                logger.warning('TP 7.1 step 2 not observed: the transfer left the staging area '
                               'before the first poll, so no staged file could be inspected')

        with allure.step('Step 3: the transfer costs disk rather than memory'):
            payload_size = os.path.getsize(local_image)
            if memory_before is not None and memory_during is not None:
                growth = memory_during - memory_before
                budget = payload_size * GnoiStaging.MEMORY_GROWTH_FRACTION
                logger.info(f'{GnoiStaging.CONTAINER} memory {memory_before} -> {memory_during} '
                            f'({growth:+} bytes) while transferring {payload_size} bytes')
                assert growth < budget, (
                    f'the container grew by {growth} bytes transferring {payload_size}: staging is '
                    'meant to be disk-backed, so it must not scale with the payload')
            else:
                logger.warning('TP 7.1 step 2 memory check skipped: MemoryCurrent is unavailable')

            out, err = client.wait_gnoic_op(put_process, cmd_time=GNOIC_TRANSFER_TIMEOUT_SEC)
            assert not gnoi_rpc_failed(out, err), \
                f'the transfer used to observe staging failed.\nout: {out}\nerr: {err}'
            assert file_exists_on_dut(dut, transferred_file), f'{transferred_file} was not created'
            verify_staging_area_clean(dut, 'after the observed transfer')
            # /home lives on /host, so this multi-GB destination has to go before the step below
            # can attribute a change in free space to the oversized transfer.
            dut.run_cmd(f'sudo rm -f {transferred_file}')

        with allure.step('Step 4: a payload larger than the staging image fails with ENOSPC'):
            create_sparse_file(oversized_payload, GnoiStaging.OVERSIZED_PAYLOAD_BYTES)
            # Without the flush, df still reflects the previous transfer's writeback and the
            # baseline reads high by whatever is left in flight.
            dut.run_cmd('sync')
            host_free_before = filesystem_free_bytes(dut, GnoiFileService.ROOT_OWNED_DIR)
            image_allocated_before = path_allocated_bytes(dut, GnoiPaths.STAGING_IMAGE)
            log_at = dut_log_position(dut, GnoiPaths.GNOID_LOG)

            out, err = client.file_put(oversized_payload, f'{home}/gnoi_oversized_{tag}.bin',
                                       cmd_time=GNOIC_TRANSFER_TIMEOUT_SEC)
            merged = f'{out or ""}\n{err or ""}'
            assert gnoi_rpc_failed(out, err), (
                f'a {GnoiStaging.OVERSIZED_PAYLOAD_BYTES} byte payload must not fit in a '
                f'{GnoiStaging.IMAGE_SIZE_BYTES} byte staging image:\n{merged}')

            # The server has to notice the bound; whether it tells the client is a separate matter.
            verify_log_records(dut, GnoiPaths.GNOID_LOG, log_at, GnoiStaging.ENOSPC_LOG_MARKER,
                               'the staging area running out of space')
            if not any(marker in merged for marker in GnoiStaging.ENOSPC_MARKERS):
                logger.warning(
                    'TP 7.1 finding: the switch logged the write failing with no space left, but '
                    f'the client was told only {merged.strip()[-120:]!r}. There is no gRPC status '
                    'and no reason, so a caller cannot distinguish a full staging area from a '
                    'network drop')

        with allure.step('Step 5: the ENOSPC is contained inside the image and staging is cleaned'):
            dut.run_cmd('sync')
            # Containment stated as the bound rather than as preallocation. The image is sparse, so
            # filling it does cost /host real blocks as it goes; what makes the transfer contained
            # is that its size is fixed, so that cost can never exceed it however much is pushed.
            # Asserting near-zero growth instead would be asserting preallocation, which this image
            # does not have.
            image_size_after = path_size_bytes(dut, GnoiPaths.STAGING_IMAGE)
            assert image_size_after == GnoiStaging.IMAGE_SIZE_BYTES, (
                f'the staging image reports {image_size_after} bytes rather than the '
                f'{GnoiStaging.IMAGE_SIZE_BYTES} it is built with, so the ceiling that is supposed '
                'to contain a transfer moved with the payload')

            image_allocated_after = path_allocated_bytes(dut, GnoiPaths.STAGING_IMAGE)
            if image_allocated_before is not None and image_allocated_after is not None:
                growth = image_allocated_after - image_allocated_before
                logger.info(f'{GnoiPaths.STAGING_IMAGE} allocation {image_allocated_before} -> '
                            f'{image_allocated_after} ({growth:+} bytes) within a '
                            f'{GnoiStaging.IMAGE_SIZE_BYTES} byte image')
                assert image_allocated_after <= GnoiStaging.IMAGE_SIZE_BYTES, (
                    f'the staging image has {image_allocated_after} bytes allocated to it, beyond '
                    f'the {GnoiStaging.IMAGE_SIZE_BYTES} it is sized at, so filling it is not '
                    'bounded by its own size')

            host_free_after = filesystem_free_bytes(dut, GnoiFileService.ROOT_OWNED_DIR)
            if host_free_before is not None and host_free_after is not None:
                lost = host_free_before - host_free_after
                logger.info(f'{GnoiFileService.ROOT_OWNED_DIR} free {host_free_before} -> '
                            f'{host_free_after} ({lost:+} bytes)')
                # Bounded by the image and not by a fixed margin, for the same reason: a transfer
                # that fills a sparse image legitimately costs /host up to the image's size. The
                # failure this catches is the payload costing more than the image it was capped by,
                # which is what escaping the image would look like.
                assert lost <= GnoiStaging.IMAGE_SIZE_BYTES, (
                    f'{GnoiFileService.ROOT_OWNED_DIR} lost {lost} bytes to a '
                    f'{GnoiStaging.OVERSIZED_PAYLOAD_BYTES} byte transfer that was supposed to be '
                    f'bounded by the {GnoiStaging.IMAGE_SIZE_BYTES} byte loop image')

            residue = staging_files(dut)
            if residue:
                logger.warning(f'TP 7.1 finding: the failed oversized transfer left staging '
                               f'residue, which the next transfer inherits: {residue}')

        with allure.step('Step 6: a second OS.Install competing with one in flight is refused'):
            install_process = client.start_os_install(local_image, version)
            in_flight = wait_for_staged_file(dut)
            assert in_flight, ('no staged file appeared for the backgrounded install, so the lock '
                               'cannot be exercised against an install in flight')

            out, err = concurrent_client.os_install(
                local_image, version, cmd_time=GnoiStaging.CONCURRENT_INSTALL_TIMEOUT_SEC)
            merged = f'{out or ""}\n{err or ""}'
            assert gnoi_rpc_failed(out, err), \
                f'a second OS.Install must be refused while one is in flight:\n{merged}'
            if GnoiStaging.INSTALL_IN_PROGRESS not in merged:
                logger.warning(f'TP 7.1 finding: the second install was refused without reporting '
                               f'{GnoiStaging.INSTALL_IN_PROGRESS}: {merged.strip()[-300:]}')

        with allure.step('Step 7: a File.Put during an install is permitted (HLD §8.1 gap)'):
            out, err = concurrent_client.file_put(small_payload, during_install_file)
            assert not gnoi_rpc_failed(out, err), (
                'File.Put is documented as not mutually excluded with OS.Install, so it must be '
                f'permitted while one is in flight.\nout: {out}\nerr: {err}')
            assert file_exists_on_dut(dut, during_install_file), \
                f'{during_install_file} was not created during the install'
            logger.info('File.Put and OS.Install ran concurrently, as HLD §8.1 documents')

            # Bounded well under the install ceiling: an install of this image takes well under a
            # minute, so anything approaching the bound is a stall worth reporting rather than
            # waiting out.
            out, err = client.wait_gnoic_op(install_process,
                                            cmd_time=GnoiStaging.CONCURRENT_INSTALL_TIMEOUT_SEC)
            verify_install_response(out, err, version)
            logger.info('the install completed normally despite the competing RPCs')
            verify_rpc_counters_increased(gnoi_rpc, baseline_counters, [GnoiRpc.OS_INSTALL])

        with allure.step('Step 8: stopping the service tears the staging area down'):
            control_gnoi_service(dut, 'stop')

            assert not staging_mount_line(dut), \
                f'{GnoiPaths.STAGING_DIR} is still mounted after the service stopped'
            assert not file_exists_on_dut(dut, GnoiPaths.STAGING_IMAGE), \
                f'{GnoiPaths.STAGING_IMAGE} survived the service stop; nothing should persist'
            for state_file in (GnoiPaths.STATE_FILE, GnoiPaths.GNMI_STATE_FILE):
                assert not file_exists_on_dut(dut, state_file), \
                    f'{state_file} survived the service stop'
            assert not process_is_running(dut, GnoiPaths.USER_LOOKUP_DAEMON), \
                f'{GnoiPaths.USER_LOOKUP_DAEMON} is still running after the service stopped'

        with allure.step('Step 9: starting it recreates an empty staging area and gNOI serves again'):
            control_gnoi_service(dut, 'start')
            assert wait_for_gnoi_to_answer(client) is not None, \
                'gNOI did not answer again after the service was restarted'

            mount_line = staging_mount_line(dut)
            assert GnoiPaths.STAGING_IMAGE in mount_line, \
                f'{GnoiPaths.STAGING_DIR} was not remounted after the restart: {mount_line!r}'
            assert path_size_bytes(dut, GnoiPaths.STAGING_IMAGE) == GnoiStaging.IMAGE_SIZE_BYTES, \
                'the recreated staging image is not the documented size'
            verify_staging_area_clean(dut, 'after the service was recreated')
            assert process_is_running(dut, GnoiPaths.USER_LOOKUP_DAEMON), \
                f'{GnoiPaths.USER_LOOKUP_DAEMON} was not restarted with the service'

        # The §9 half. Last, because the reboot costs minutes and everything above is cheaper.
        health_baseline = system_health_issues(system)
        if health_baseline:
            logger.warning(f'health issues already present before the reboot, unrelated to gNOI: '
                           f'{health_baseline}')

        with allure.step(f'Step 10: gNOI survives an {GnoiStaging.REBOOT_MODE_IMMEDIATE} reboot'):
            # 'immediate' rather than 'cold' on purpose: it takes the OS down without stopping
            # services, so the staging image and its mount are left behind and the control script
            # has to rebuild over a stale one. A cold reboot shuts nv-gnmi down properly, which is
            # the path steps 8 and 9 already cover.
            _verify_gnoi_survives_reboot(system, dut, client, topology_obj,
                                         GnoiStaging.REBOOT_MODE_IMMEDIATE, health_baseline)
            logger.warning('TP §9 note: warm and fast reboots do not exist on NVOS - '
                           "'nv action reboot system' accepts only halt, cold, immediate and "
                           'power-cycle, so those two objectives are not applicable')

    finally:
        # Before the file cleanup: a switch left with gNOI stopped breaks every test that follows,
        # where leftover files only cost disk.
        with allure.step('Cleanup: put the service back in the state the test found it'):
            try:
                restore_gnoi_service(dut, service_state_before, client)
            except BaseException as restore_error:
                logger.warning(f'could not restore {GnoiStaging.SERVICE}: {restore_error}')

        with allure.step('Cleanup: remove transferred files and the installed image'):
            try:
                dut.run_cmd(f'sudo rm -f {transferred_file} {during_install_file} '
                            f'{home}/gnoi_oversized_{tag}.bin')
                # The install above leaves a ~3GB image on /host that gNOI itself never deletes.
                dut.run_cmd(f'sudo rm -f {GnoiOsService.IMAGE_DIR}/{stored_image}')
                for path in (oversized_payload, small_payload):
                    if os.path.exists(path):
                        os.remove(path)
            except BaseException as cleanup_error:
                logger.warning(f'cleanup did not complete: {cleanup_error}')
