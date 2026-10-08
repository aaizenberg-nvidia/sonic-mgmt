"""
gNOI OS upgrade composition (tp/gnoi_TP.md §5.2).

Drives a NOS image through OS.Install, OS.Activate and System.Reboot, and checks the boundary the
HLD draws between them: Install only transfers, Activate performs the partition work and selects
boot-next, Reboot applies it. Also covers the deterministic 'image-<version>.bin' naming, the new
'boot-next ... other' enum, and the documented fact that gNOI never deletes a fetched image.

Three things differ from the TP text, for reasons the switch imposes:
  - Counters for OS.Install and OS.Activate are read before the reboot, not in a single check at
    the end. nv-gnoid keeps them in /run, which does not survive a reboot, so the RPCs that caused
    the reboot cannot be counted after it. System.Reboot is therefore reported, not asserted.
  - 'boot-next other' only runs when a second partition exists; on a switch holding a single image
    the enum has nothing to resolve to.
  - The image may already be the running version, in which case the server is entitled to answer
    Validated without transferring. The transfer-shaped assertions are then reported as skipped
    rather than failed, and the run still covers activate, reboot and retention.
  - System.Reboot is not expected to answer cleanly. The server goes down with the RPC in flight,
    so the client sees Unavailable/EOF; the reboot is therefore judged by the switch going down and
    coming back, and the RPC itself only has to avoid being refused.

Before the reboot the test always points boot-next back at the partition holding the image under
test, so a run cannot leave the switch booting into an older image.
"""
import logging
import os
import time

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ImageConsts
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, RebootParams
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnoi.GnoiClient import GnoiClient
from ngts.tests_nvos.system.gnoi.constants import (
    GNOIC_OS_ACTIVATE_TIMEOUT_SEC,
    GNOIC_OS_INSTALL_TIMEOUT_SEC,
    GnoicErr,
    GnoiOsService,
    GnoiRebootMode,
    GnoiRpc,
)
from ngts.tests_nvos.system.gnoi.helpers import (
    build_id_for_version,
    effective_boot_next_partition,
    get_gnoi_rpc_counters,
    get_image_state,
    gnoi_rpc_failed,
    images_on_dut,
    installer_version,
    partition_build_id,
    partition_holding_version,
    resolve_local_os_image,
    system_health_issues,
    verify_dut_did_not_reboot,
    verify_gnoi_reachable,
    verify_install_response,
    verify_staging_area_clean,
    get_dut_uptime_sec,
    wait_for_reported_boot_next,
)

logger = logging.getLogger()


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.gnoi
def test_gnoi_os_install_activate_reboot(engines, devices, topology_obj, test_name, target_version):
    """
    Validate the OS.Install / OS.Activate / System.Reboot composition.

    Test flow (tp/gnoi_TP.md §5.2):
        1.  Record the baseline: partitions, boot-next, running version, images on disk
        2.  OS.Install streams the image and ends in Validated
        3.  The image is stored as 'image-<version>.bin', named from the RPC's version field
        4.  Partitions and boot-next are untouched - Install only transferred
        5.  The staging area is empty once the RPC is done
        6.  OS.Activate --no-reboot succeeds without rebooting the switch
        7.  Boot-next now points at a partition holding the activated version
        8.  'nv action boot-next system image other' resolves to the non-running partition
        9.  Received counters moved for OS.Install and OS.Activate (read before the reboot)
        10. System.Reboot COLD reboots the switch and it comes back functional
        11. The running version is the activated one and gNOI answers again
        12. The transferred image is still on disk - gNOI never deletes it
        13. Re-activating the already-installed image succeeds
        14. Install and activate durations are recorded (no numeric target, per TP §8)
    """
    system = System()
    dut = engines.dut
    gnoi_rpc = system.gnmi_server.status.gnoi_rpc

    client = GnoiClient(dut.ip,
                        username=devices.dut.default_username,
                        password=devices.dut.default_password,
                        verify_tools_installed=True)

    local_image = resolve_local_os_image(target_version)
    version = installer_version(local_image)
    expected_build_id = build_id_for_version(version)
    stored_name = GnoiOsService.STORED_NAME_TEMPLATE.format(version=version)
    image_size = os.path.getsize(local_image)

    install_duration = activate_duration = None
    # Set by the handler below so the cleanup can tell a cleanup failure from a test failure, and
    # not replace a real failure with its own.
    test_failed = False

    with allure.step('Step 1: record the baseline image state'):
        baseline_state = get_image_state(system)
        baseline_running = system.version.get_nvos_image_version()
        baseline_files = images_on_dut(dut)
        baseline_counters = get_gnoi_rpc_counters(gnoi_rpc)
        logger.info(f'installing {local_image} ({image_size} bytes) as version {version}, '
                    f'expecting build-id {expected_build_id}')
        logger.info(f'baseline: running={baseline_running}, state={baseline_state}, '
                    f'images={baseline_files}')
        # A same-version run cannot show a version change across the reboot; say so up front so the
        # weaker assertions below are not mistaken for the test passing on a real upgrade.
        same_version = baseline_running == expected_build_id
        if same_version:
            logger.warning(f'the switch already runs {expected_build_id}: this run covers the '
                           'transfer, activation and reboot paths but cannot observe a version change')

    try:
        with allure.step(f'Step 2: OS.Install streams the image and ends in '
                         f'{GnoiOsService.VALIDATED}'):
            started = time.time()
            out, err = client.os_install(local_image, version,
                                         cmd_time=GNOIC_OS_INSTALL_TIMEOUT_SEC)
            install_duration = time.time() - started
            transferred = verify_install_response(out, err, version)
            logger.info(f'OS.Install took {install_duration:.0f}s')

        with allure.step(f'Step 3: the image is stored as {stored_name}'):
            files_after_install = images_on_dut(dut)
            logger.info(f'images on the switch after OS.Install: {files_after_install}')
            if transferred:
                assert stored_name in files_after_install, (
                    f'OS.Install stored no {stored_name}; the name must come from the RPC version '
                    f'field, not the client filename {os.path.basename(local_image)}. '
                    f'Directory holds: {files_after_install}')
            else:
                logger.warning('TP 5.2 step 3 not covered: the server answered Validated without '
                               'transferring, so it stored nothing to name')

        with allure.step('Step 4: partitions and boot-next are untouched by the install'):
            state_after_install = get_image_state(system)
            assert state_after_install == baseline_state, (
                f'OS.Install changed the image state, but it only transfers a file.\n'
                f'before: {baseline_state}\nafter:  {state_after_install}')

        with allure.step('Step 5: the staging area is empty once the install is done'):
            verify_staging_area_clean(dut, 'after OS.Install')

        with allure.step('Step 6: OS.Activate --no-reboot does not reboot the switch'):
            uptime_before = get_dut_uptime_sec(dut)
            started = time.time()
            out, err = client.os_activate(version, no_reboot=True,
                                          cmd_time=GNOIC_OS_ACTIVATE_TIMEOUT_SEC)
            activate_duration = time.time() - started
            assert not gnoi_rpc_failed(out, err), f'OS.Activate failed.\nout: {out}\nerr: {err}'
            verify_dut_did_not_reboot(dut, uptime_before)
            logger.info(f'OS.Activate took {activate_duration:.0f}s')

        with allure.step(f'Step 7: boot-next points at a partition holding {expected_build_id}'):
            state_after_activate = get_image_state(system)
            logger.info(f'image state after OS.Activate: {state_after_activate}')
            # Grub, not the CLI: see effective_boot_next_partition for why the two disagree.
            next_partition = effective_boot_next_partition(dut, system)
            assert next_partition, 'could not determine what the switch will boot next'
            assert partition_build_id(state_after_activate, next_partition) == expected_build_id, (
                f'boot-next is {next_partition}, which holds '
                f'{partition_build_id(state_after_activate, next_partition)!r} rather than the '
                f'activated {expected_build_id}')

        with allure.step("Step 8: 'boot-next ... other' resolves to the non-running partition"):
            running_partition = ImageConsts.PARTITION + \
                str(state_after_activate[ImageConsts.CURRENT_IMG])
            other_partition = (ImageConsts.PARTITION2_IMG if running_partition ==
                               ImageConsts.PARTITION1_IMG else ImageConsts.PARTITION1_IMG)

            if partition_build_id(state_after_activate, other_partition):
                system.image.action_boot_next(ImageConsts.OTHER).verify_result()
                selected = effective_boot_next_partition(dut, system)
                assert selected == other_partition, (
                    f"'{ImageConsts.OTHER}' pointed grub at {selected}, but the non-running "
                    f'partition is {other_partition}')

                # The CLI is expected to agree eventually rather than immediately, and how long it
                # takes is the finding: anyone who sets boot-next and reads it back to confirm gets
                # the previous value for about a minute.
                lag = wait_for_reported_boot_next(system, other_partition)
                assert lag is not None, (
                    f"'nv show system image' never reported boot-next as {other_partition} within "
                    f'{GnoiOsService.BOOT_NEXT_VISIBILITY_TIMEOUT_SEC}s, though grub holds it')
                logger.warning(f'TP 5.2 finding: boot-next took {lag:.0f}s to appear in '
                               "'nv show system image', which reported the old partition until "
                               'then. The action itself is immediate - grub was updated before the '
                               'command returned')

                # Put boot-next back on the image under test: 'other' may well point at an older
                # build, and the reboot below must not land the switch on it.
                target_partition = partition_holding_version(state_after_activate, expected_build_id)
                assert target_partition, (f'no partition holds {expected_build_id} after '
                                          f'activation: {state_after_activate}')
                system.image.action_boot_next(target_partition).verify_result()
                assert effective_boot_next_partition(dut, system) == target_partition, (
                    f'boot-next could not be restored to {target_partition}, so the reboot below '
                    'would land on the wrong image')
            else:
                logger.warning(f'TP 5.2 step 8 not covered: the switch holds one image only, so '
                               f"'{ImageConsts.OTHER}' has no partition to resolve to")

        # Read before the reboot: nv-gnoid keeps its counters under /run, which the reboot wipes.
        with allure.step('Step 9: received counters moved for OS.Install and OS.Activate'):
            counters = get_gnoi_rpc_counters(gnoi_rpc)
            for rpc_name in (GnoiRpc.OS_INSTALL, GnoiRpc.OS_ACTIVATE):
                before = baseline_counters.get(rpc_name, (0, 0))[0]
                after = counters.get(rpc_name, (0, 0))[0]
                assert after > before, \
                    f'{rpc_name} received count did not move: {before} -> {after}'
                logger.info(f'{rpc_name} received: {before} -> {after}')

        with allure.step(f'Step 10: System.Reboot {GnoiRebootMode.COLD} reboots the switch'):
            # Judged against what the switch was already complaining about: juliet-128 reports a
            # coolant-leak sensor on every boot, and this test should catch what the reboot broke
            # rather than inherit that.
            health_before_reboot = system_health_issues(system)
            if health_before_reboot:
                logger.warning('health issues already present before the reboot, unrelated to '
                               f'gNOI: {health_before_reboot}')
            out, err = client.system_reboot(GnoiRebootMode.COLD)
            # A reboot rarely answers cleanly: the server usually goes down with the RPC still in
            # flight, which reaches the client as Unavailable/EOF. That is indistinguishable from
            # an unreachable server, so the reboot is judged by the switch's behaviour below and
            # the RPC only has to not be *refused*.
            merged = f'{out or ""}\n{err or ""}'
            for refusal in (GnoicErr.INVALID_ARGUMENT, GnoicErr.UNIMPLEMENTED,
                            GnoicErr.UNAUTHENTICATED, GnoicErr.PERMISSION_DENIED):
                assert refusal not in merged, (
                    f'System.Reboot {GnoiRebootMode.COLD} was refused with {refusal}, and '
                    f'{GnoiRebootMode.COLD} is a supported mode:\n{merged}')
            if gnoi_rpc_failed(out, err):
                logger.info('the reboot RPC did not answer cleanly, which is expected when the '
                            f'server goes down with it in flight:\n{merged}')
            # wait_on_system_reboot and not wait_for_nvos_to_become_functional directly: that one
            # assumes the switch is already reachable, and its innermost wait allows about two
            # minutes where a cold reboot here takes around six, so it gave up before the switch
            # had begun to boot. This waits for the port to drop and come back, and for the serial
            # 'System is ready' banner on the platform's own timeout, before checking readiness.
            # Unhealthy does not throw: the switch's pre-existing complaints are subtracted below.
            reboot_params = RebootParams(topology_obj=topology_obj,
                                         throw_exception_on_unhealthy=False)
            DutUtilsTool.wait_on_system_reboot(dut, reboot_params=reboot_params,
                                               device=devices.dut)
            new_issues = system_health_issues(system) - health_before_reboot
            assert not new_issues, \
                f'the reboot left the switch with new health issues: {new_issues}'

        with allure.step(f'Step 11: the switch runs {expected_build_id} and gNOI answers again'):
            running = system.version.get_nvos_image_version()
            assert running == expected_build_id, \
                f'the switch came back running {running}, not the activated {expected_build_id}'
            verify_gnoi_reachable(client)
            # The counters are a fresh collection: the state file went with the reboot.
            logger.info(f'gnoi-rpc after the reboot: {get_gnoi_rpc_counters(gnoi_rpc)}')

        with allure.step('Step 12: the transferred image is still on disk'):
            if transferred:
                assert stored_name in images_on_dut(dut), (
                    f'{stored_name} is gone after the reboot; gNOI never deletes a fetched image, '
                    'so it must still be there for the operator to clean up')

        with allure.step('Step 13: re-activating the already-installed image succeeds'):
            out, err = client.os_activate(version, no_reboot=True,
                                          cmd_time=GNOIC_OS_ACTIVATE_TIMEOUT_SEC)
            assert not gnoi_rpc_failed(out, err), (
                'OS.Activate of an already-installed image must succeed - NVUE @install is a no-op '
                f'and @boot-next does the real work.\nout: {out}\nerr: {err}')

        with allure.step('Step 14: record the install and activate durations'):
            # Informational: TP §8 sets no numeric target for either.
            logger.info(f'OS.Install of {image_size} bytes: {install_duration:.0f}s. '
                        f'OS.Activate: {activate_duration:.0f}s')

    except BaseException:
        test_failed = True
        raise

    finally:
        with allure.step('Cleanup: leave the switch booting the image it is running'):
            should_boot = booting = None
            try:
                state = get_image_state(system)
                should_boot = ImageConsts.PARTITION + str(state[ImageConsts.CURRENT_IMG])
                # Grub rather than the reported `next`, which can still be showing the previous
                # value here and would have the cleanup either skip a fix or undo a correct one.
                if effective_boot_next_partition(dut, system) != should_boot:
                    system.image.action_boot_next(should_boot).verify_result()
                # Read back: the action reporting success is not proof grub took it.
                booting = effective_boot_next_partition(dut, system)
                logger.info(f'final image state: {get_image_state(system)}')
            except BaseException as cleanup_error:
                logger.error(f'could not restore the image state: {cleanup_error}')

            try:
                # Retention is asserted in step 12; the copy is dropped here so repeated runs do
                # not fill /host with ~3GB images. Separate from the restore above so that a
                # failure in either one still leaves the other attempted.
                dut.run_cmd(f'sudo rm -f {GnoiOsService.IMAGE_DIR}/{stored_name}')
            except BaseException as remove_error:
                logger.warning(f'could not remove {stored_name}: {remove_error}')

            if booting is None or booting != should_boot:
                detail = (f'the switch is left booting {booting!r} rather than the running '
                          f'{should_boot!r}, so its next reboot lands on the wrong image and every '
                          'test that follows on this switch runs against it')
                if test_failed:
                    # Raising here would replace the failure the test actually found. Attached so
                    # it reaches the report rather than only the log.
                    logger.error(f'cleanup finding: {detail}')
                    allure.attach('cleanup could not restore boot-next', detail)
                else:
                    raise AssertionError(detail)
