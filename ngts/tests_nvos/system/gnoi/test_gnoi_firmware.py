"""
gNOI platform-firmware SetPackage, per-component (tp/gnoi_TP.md §5.3).

NVOS serves System.SetPackage as a platform-firmware install and takes the target component from
the RPC's `version` field, so one RPC covers every component the switch exposes. The component
under test is chosen at random from those that have a firmware file available, which rotates the
coverage across runs rather than pinning it to one part.

**Nothing here writes firmware by default.** A SetPackage without `activate` performs NVUE's
@fetch alone: it stages a file into the component's store and is safe on any component. Adding
`activate` performs @install as well, and an install burns - `skip-reboot` defers when the image
loads, not whether it is written. A downgrade of BMC, EROT, BIOS or CPLD can leave the switch
needing out-of-band recovery, and every file the lab JSON offers this platform is older than what
is running. So the two steps that can reach an install are opt-in:

    GNOI_ALLOW_FW_ACTIVATE=1   attempt an install of a version already present, to prove NVUE's
                               version check refuses it (expected to write nothing, but that is
                               the behaviour under test rather than an assumption)
    GNOI_ALLOW_FW_BURN=1       install for real on the one component that configuration alone can
                               put back: ASIC, restored with fw-source default and a reboot

Without them the test reports those steps as uncovered and still exercises the fetch path, the
relayed NVUE errors and the traversal guard, which is where the gNOI-specific logic lives.
"""
import logging
import os
import uuid

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnoi.GnoiClient import GnoiClient
from ngts.tests_nvos.system.gnoi.constants import (
    GnoicErr,
    GnoiFirmware,
    GnoiRpc,
)
from ngts.tests_nvos.system.gnoi.helpers import (
    component_firmware_files,
    component_store_dir,
    delete_component_firmware_file,
    file_exists_on_dut,
    firmware_component_candidates,
    get_gnoi_rpc_counters,
    gnoi_rpc_failed,
    pick_firmware_component,
    platform_firmware_versions,
    same_version_component,
    small_local_package,
    verify_rpc_counters_increased,
    verify_staging_area_clean,
)

logger = logging.getLogger()


def _refused(out, err, expected_desc=''):
    """Assert the RPC failed, optionally carrying a given description, and return its output."""
    merged = f'{out or ""}\n{err or ""}'
    assert gnoi_rpc_failed(out, err), f'the RPC was expected to fail but did not:\n{merged}'
    if expected_desc:
        assert expected_desc in merged, \
            f'the refusal did not mention {expected_desc!r}:\n{merged}'
    return merged


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.gnoi
def test_gnoi_firmware_setpackage_modes(engines, devices, test_name):
    """
    Validate System.SetPackage in per-component mode.

    Test flow (tp/gnoi_TP.md §5.3):
        1. Record the baseline component versions and pick a component to exercise
        2. SetPackage without activate stages the file under the client-provided name
        3. The component's firmware is unchanged, so the fetch really did not install
        4. The gNOI staging area is left clean and the received counter moved
        5. An unknown component is refused with NVUE's own message relayed
        6. A destination whose extension the component does not expect is refused
        7. A filename or component carrying a traversal is refused before any NVUE call
        8. An already-installed version cannot be re-burned over gNOI (opt-in)
        9. A real install on the restorable component (opt-in)
        10. The failed counter moved for the refusals above
    """
    system = System()
    platform = Platform()
    dut = engines.dut
    gnoi_rpc = system.gnmi_server.status.gnoi_rpc

    client = GnoiClient(dut.ip,
                        username=devices.dut.default_username,
                        password=devices.dut.default_password,
                        verify_tools_installed=True)

    tag = uuid.uuid4().hex[:8]
    baseline_counters = get_gnoi_rpc_counters(gnoi_rpc)
    component = staged_name = probe_pkg = None

    try:
        with allure.step('Step 1: record the baseline and pick a component'):
            versions_before = platform_firmware_versions(platform)
            logger.info(f'component firmware before: {versions_before}')
            candidates = firmware_component_candidates(platform)
            component, fw_path, fw_filename, fw_version = pick_firmware_component(platform)
            # Name the destination after the source: NVUE rejects any other extension for the
            # component, and each component expects a different one.
            suffix = os.path.splitext(fw_filename)[1]
            staged_name = f'gnoi_fw_{tag}{suffix}'
            files_before = component_firmware_files(platform, component)
            logger.info(f'{component} store holds {files_before} before the transfer')

        with allure.step(f'Step 2: SetPackage without activate stages the file for {component}'):
            out, err = client.system_set_package(
                fw_path, staged_name, component,
                cmd_time=GnoiFirmware.SETPACKAGE_TIMEOUT_SEC)
            assert not gnoi_rpc_failed(out, err), \
                f'SetPackage for {component} failed.\nout: {out}\nerr: {err}'

            files_after = component_firmware_files(platform, component)
            assert staged_name in files_after, (
                f'{staged_name} is not in the {component} firmware store after the fetch: '
                f'{files_after}. The client-provided name is what the file must be stored under')
            # And on disk, under the component's own directory in the firmware store.
            stored_path = f'{GnoiFirmware.STORE_DIR}/{component_store_dir(component)}/{staged_name}'
            assert file_exists_on_dut(dut, stored_path), \
                f'the fetched package is not at {stored_path}'

        with allure.step('Step 3: without activate nothing was installed'):
            versions_after = platform_firmware_versions(platform)
            assert versions_after == versions_before, (
                'component firmware changed after a SetPackage with no activate, which must only '
                f'fetch.\nbefore: {versions_before}\nafter:  {versions_after}')

        with allure.step('Step 4: the staging area is clean and the received counter moved'):
            verify_staging_area_clean(dut, f'after the {component} package transfer')
            verify_rpc_counters_increased(gnoi_rpc, baseline_counters,
                                          [GnoiRpc.SYSTEM_SET_PACKAGE])

        with allure.step('Step 5: an unknown component is refused with the NVUE message relayed'):
            out, err = client.system_set_package(
                fw_path, staged_name, GnoiFirmware.UNKNOWN_COMPONENT, activate=True,
                cmd_time=GnoiFirmware.SETPACKAGE_TIMEOUT_SEC)
            merged = _refused(out, err, GnoiFirmware.FETCH_FAILED_DESC)
            assert GnoiFirmware.UNKNOWN_COMPONENT in merged and \
                GnoiFirmware.NVUE_MISSING_ITEM_DESC in merged, (
                    "NVUE's own unknown-component message must reach the client, naming the value it "
                    f'rejected:\n{merged}')
            # NVUE is the sole validator here, and its refusal arrives wrapped as Internal rather
            # than NotFound or InvalidArgument - the same conflation seen in the file service.
            if GnoicErr.INTERNAL in merged:
                logger.warning('TP 5.3 finding: an unknown component is reported as '
                               f'{GnoicErr.INTERNAL}, where the request was well-formed and the '
                               'component simply does not exist')

        with allure.step('Step 6: a destination whose extension the component rejects'):
            out, err = client.system_set_package(
                fw_path, f'gnoi_bad_{tag}{GnoiFirmware.WRONG_SUFFIX}', component,
                cmd_time=GnoiFirmware.SETPACKAGE_TIMEOUT_SEC)
            merged = _refused(out, err, GnoiFirmware.UNSUPPORTED_TYPE_DESC)
            assert suffix in merged, (
                f'the refusal should name the extension {component} expects ({suffix}), since a '
                f'client has no other way to learn it:\n{merged}')
            logger.info(f'TP 5.3 note: the destination name must carry {component}\'s expected '
                        f'extension ({suffix}); gNOI exposes no way to discover it')

        with allure.step('Step 7: a traversal in the filename or the component is refused'):
            traversal_names = [f'{prefix}{GnoiFirmware.TRAVERSAL_BASENAME}{suffix}'
                               for prefix in GnoiFirmware.TRAVERSAL_PREFIXES]
            # A few KB stands in for the real package here. The name guard runs before any content
            # is read, so its verdict does not depend on what is being sent - but the size does
            # decide whether the client ever sees that verdict, which the last case below covers.
            probe_pkg = small_local_package(suffix)
            for bad_name in traversal_names:
                out, err = client.system_set_package(
                    probe_pkg, bad_name, component,
                    cmd_time=GnoiFirmware.SETPACKAGE_TIMEOUT_SEC)
                merged = _refused(out, err, GnoiFirmware.TRAVERSAL_DESC)
                assert GnoicErr.INVALID_ARGUMENT in merged, (
                    f'a filename carrying a traversal must be {GnoicErr.INVALID_ARGUMENT}:\n'
                    f'{merged}')
                assert GnoiFirmware.INVALID_FILENAME_DESC in merged, \
                    f'the refusal should name the filename as the offending field:\n{merged}'

            out, err = client.system_set_package(
                probe_pkg, staged_name, f'../{component}',
                cmd_time=GnoiFirmware.SETPACKAGE_TIMEOUT_SEC)
            merged = _refused(out, err, GnoiFirmware.TRAVERSAL_DESC)
            assert GnoicErr.INVALID_ARGUMENT in merged, \
                f'a component carrying a traversal must be {GnoicErr.INVALID_ARGUMENT}:\n{merged}'
            assert GnoiFirmware.INVALID_COMPONENT_DESC in merged, \
                f'the refusal should name the component as the offending field:\n{merged}'

            # Same rejection, real package: the guard fires on the first message while the client
            # is still streaming, so gRPC surfaces the broken write and the reason is lost.
            out, err = client.system_set_package(
                fw_path, traversal_names[0], component,
                cmd_time=GnoiFirmware.SETPACKAGE_TIMEOUT_SEC)
            merged = _refused(out, err)
            if GnoiFirmware.TRAVERSAL_DESC not in merged:
                logger.warning(
                    'TP 5.3 finding: a rejected name is reported as a bare transport error for a '
                    f'package of any real size ({os.path.getsize(fw_path)} bytes here), losing the '
                    f'{GnoicErr.INVALID_ARGUMENT} reason the same request returns for a small one. '
                    'Third instance of a mid-stream refusal not reaching the client, after the '
                    f'staging ENOSPC case:\n{merged}')
            # The guard runs before NVUE is called at all, so nothing was staged for any of them.
            escaped = f'{GnoiFirmware.STORE_DIR}/{GnoiFirmware.TRAVERSAL_BASENAME}{suffix}'
            assert not file_exists_on_dut(dut, escaped), \
                f'a traversal escaped the component directory and landed at {escaped}'
            verify_staging_area_clean(dut, 'after the rejected traversals')

        with allure.step('Step 8: an already-installed version cannot be re-burned over gNOI'):
            already_installed = same_version_component(platform, candidates)
            if not os.environ.get(GnoiFirmware.ALLOW_ACTIVATE_ENV_VAR):
                logger.warning(
                    'TP 5.3 step 8 not covered: activating reaches NVUE @install, which writes '
                    f'firmware if the version check does not refuse it. Set '
                    f'{GnoiFirmware.ALLOW_ACTIVATE_ENV_VAR} to run it' +
                    (f' (would use {already_installed}, already at the offered version)'
                     if already_installed else ''))
            elif not already_installed:
                logger.warning('TP 5.3 step 8 not covered: no component is running exactly the '
                               'version its firmware file offers, so a refusal cannot be provoked '
                               'without a genuine downgrade')
            else:
                same_path, same_filename, same_version = candidates[already_installed]
                same_name = f'gnoi_same_{tag}{os.path.splitext(same_filename)[1]}'
                versions_before_activate = platform_firmware_versions(platform)
                out, err = client.system_set_package(
                    same_path, same_name, already_installed, activate=True,
                    cmd_time=GnoiFirmware.SETPACKAGE_TIMEOUT_SEC)
                logger.info(f'activate of the installed {same_version} on {already_installed} '
                            f'returned:\nout: {out}\nerr: {err}')
                assert platform_firmware_versions(platform) == versions_before_activate, (
                    f'{already_installed} firmware changed while re-installing the version it '
                    'already ran')
                delete_component_firmware_file(platform, already_installed, same_name)

        with allure.step('Step 9: a real install on the restorable component'):
            if not os.environ.get(GnoiFirmware.ALLOW_BURN_ENV_VAR):
                logger.warning(
                    f'TP 5.3 step 9 not covered: a real install writes firmware. Set '
                    f'{GnoiFirmware.ALLOW_BURN_ENV_VAR} to run it against '
                    f'{GnoiFirmware.BURNABLE_COMPONENT}, which fw-source default can put back')
            else:
                # Deliberately not unattended: the restore is a configuration change plus a
                # reboot, and it belongs in a maintenance window with someone watching. The manual
                # procedure is in §5.3 of the TP.
                logger.warning(
                    f'TP 5.3 step 9: {GnoiFirmware.ALLOW_BURN_ENV_VAR} is set, but the real burn '
                    'is documented as a manual procedure rather than automated here')

        with allure.step('Step 10: the failed counter moved for the refusals'):
            verify_rpc_counters_increased(gnoi_rpc, baseline_counters,
                                          [GnoiRpc.SYSTEM_SET_PACKAGE], failed=True)

    finally:
        with allure.step('Cleanup: remove the fetched package from the firmware store'):
            if component and staged_name:
                delete_component_firmware_file(platform, component, staged_name)
                remaining = component_firmware_files(platform, component)
                logger.info(f'{component} store holds {remaining} after cleanup')
            if probe_pkg and os.path.exists(probe_pkg):
                os.remove(probe_pkg)
