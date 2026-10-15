"""
gNOI file service lifecycle (tp/gnoi_TP.md §5.1).

Validates the new '/system/file-path' NVUE resource and all four gNOI File RPCs end to end: the
NVUE list/fetch/upload/delete actions, a put/stat/get/remove round trip through gnoic, per-user
ownership of what the server writes, staging cleanup between transfers, and retrieval of a
tech-support dump.

Two TP steps were written against paths the switch does not allow, and are implemented against what
it does:
  - The TP transfers into /host. That directory is root-owned drwxr-xr-x and File RPCs run as the
    requesting user, so a write-role admin cannot create anything there; the NVUE fetch action
    rejects such a destination outright with 'File Operation Error'. Transfers therefore target the
    user's home directory, which the user owns.
  - The TP passes the URI as 'uri <source-uri>'. The actions take <path> and <uri> positionally.
"""
import logging
import os
import shlex
import shutil
import stat
import tempfile
import uuid

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnoi.GnoiClient import GnoiClient
from ngts.tests_nvos.system.gnoi.constants import (
    GNOIC_TRANSFER_TIMEOUT_SEC,
    GnoicErr,
    GnoiFileService,
    GnoiPaths,
    GnoiRpc,
)
from ngts.tests_nvos.system.gnoi.helpers import (
    dut_file_attributes,
    dut_file_md5,
    file_exists_on_dut,
    find_listing_entry,
    get_gnoi_rpc_counters,
    gnoi_rpc_failed,
    local_file_md5,
    parse_nvue_file_list,
    single_retrieved_file,
    verify_refusal_status,
    verify_rpc_counters_increased,
    verify_staging_area_clean,
)

logger = logging.getLogger()

FILE_CONTENT = b'gNOI file service lifecycle payload\n'


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.gnoi
def test_gnoi_file_service_lifecycle(engines, devices, test_name):
    """
    Validate the file-path NVUE actions and the four File RPCs.

    Test flow (tp/gnoi_TP.md §5.1):
        1.  'nv action list system file-path' returns metadata for a file and for a directory
        2.  'nv action fetch system file-path <path> <uri> file-permissions 644' writes mode 644
        3.  'nv action upload system file-path <path> <uri>' copies the file out
        4.  'nv action delete system file-path <path>' removes it; the listing no longer finds it
        5.  File.Put with permission 644 lands owned by the requesting user
        6.  File.Stat reports the path and size
        7.  File.Get returns the bytes intact (md5 against the switch's own checksum)
        8.  File.Put with permission 0 inherits the local file's mode
        9.  The staging area is empty once the transfers are done
        10. A tech-support dump can be stat-ed and retrieved intact
        11. File.Remove of a dump in root-owned /host/dump is refused
        12. File.Remove deletes the user's own file; the following stat fails
        13. Received counters moved for File.Put, File.Stat, File.Get and File.Remove
    """
    system = System()
    file_path = system.file_path
    gnoi_rpc = system.gnmi_server.status.gnoi_rpc
    dut = engines.dut
    username = devices.dut.default_username
    home = GnoiFileService.user_home(username)

    client = GnoiClient(dut.ip,
                        username=username,
                        password=devices.dut.default_password,
                        verify_tools_installed=True)

    # A per-run suffix keeps reruns and parallel setups from colliding over the same names.
    tag = uuid.uuid4().hex[:8]
    source_on_dut = f'{home}/gnoi_src_{tag}.txt'
    fetched_on_dut = f'{home}/gnoi_fetched_{tag}.txt'
    uploaded_on_dut = f'{home}/gnoi_uploaded_{tag}.txt'
    put_on_dut = f'{home}/gnoi_put_{tag}.txt'
    inherited_on_dut = f'{home}/gnoi_inherit_{tag}.txt'
    dut_files = [source_on_dut, fetched_on_dut, uploaded_on_dut, put_on_dut, inherited_on_dut]

    local_dir = tempfile.mkdtemp(prefix='gnoi_file_service_')
    local_file = os.path.join(local_dir, f'gnoi_local_{tag}.txt')
    with open(local_file, 'wb') as handle:
        handle.write(FILE_CONTENT)
    os.chmod(local_file, GnoiFileService.LOCAL_FILE_MODE)
    local_mode = oct(stat.S_IMODE(os.stat(local_file).st_mode))[2:]
    generated_dump = None

    try:
        # --- Step 1: NVUE listing ---
        with allure.step('Step 1: file-path listing returns metadata for a file and a directory'):
            probe_listing = parse_nvue_file_list(
                file_path.action_list(GnoiPaths.STAT_PROBE_PATH).verify_result())
            probe_entry = find_listing_entry(probe_listing, GnoiPaths.STAT_PROBE_PATH)
            assert probe_entry, (f'{GnoiPaths.STAT_PROBE_PATH} is missing from its own listing: '
                                 f'{probe_listing}')
            for key in (GnoiFileService.LIST_SIZE_KEY, GnoiFileService.LIST_FLAGS_KEY,
                        GnoiFileService.LIST_OWNER_KEY):
                assert key in probe_entry, f'the listing carries no "{key}": {probe_entry}'

            dir_listing = parse_nvue_file_list(file_path.action_list(home).verify_result())
            assert len(dir_listing) >= 2, (f'a directory listing of {home} should hold at least '
                                           f'"." and "..": {dir_listing}')

        # --- Step 2: NVUE fetch ---
        with allure.step(f'Step 2: fetch onto the switch with file-permissions '
                         f'{GnoiFileService.DEFAULT_FETCH_PERMISSIONS}'):
            dut.run_cmd(f'echo {shlex.quote(FILE_CONTENT.decode().rstrip(chr(10)))} > '
                        f'{source_on_dut}')
            assert file_exists_on_dut(dut, source_on_dut), \
                f'could not seed the fetch source {source_on_dut}'

            file_path.action_fetch(
                fetched_on_dut,
                f'{GnoiFileService.LOCAL_URI_SCHEME}{source_on_dut}',
                file_permissions=GnoiFileService.DEFAULT_FETCH_PERMISSIONS
            ).verify_result(expected_value=GnoiFileService.FETCH_SUCCESS_MARKER)
            mode, owner, _ = dut_file_attributes(dut, fetched_on_dut)
            assert mode == GnoiFileService.DEFAULT_FETCH_PERMISSIONS, (
                f'{fetched_on_dut} has mode {mode}, expected '
                f'{GnoiFileService.DEFAULT_FETCH_PERMISSIONS}')
            logger.info(f'fetched {fetched_on_dut} mode={mode} owner={owner}')

        # --- Step 3: NVUE upload ---
        with allure.step('Step 3: upload the file back out through the file-path action'):
            file_path.action_upload(
                fetched_on_dut,
                f'{GnoiFileService.LOCAL_URI_SCHEME}{uploaded_on_dut}').verify_result()
            assert file_exists_on_dut(dut, uploaded_on_dut), \
                f'the upload action reported success but {uploaded_on_dut} does not exist'

        # --- Step 4: NVUE delete ---
        with allure.step('Step 4: delete the uploaded file; the listing no longer finds it'):
            file_path.action_delete(uploaded_on_dut).verify_result()
            assert not file_exists_on_dut(dut, uploaded_on_dut), \
                f'{uploaded_on_dut} still exists after the delete action'
            # Listing a path that is gone is a failed action, so the expected failure is declared
            # rather than asserted after the fact - an unconsumed failed result fails the test.
            file_path.action_list(uploaded_on_dut).verify_result(
                should_succeed=False, expected_value=GnoiFileService.LIST_MISSING_MARKER)

        # Counters are sampled here so the baseline covers exactly the gnoic RPCs below.
        baseline = get_gnoi_rpc_counters(gnoi_rpc)
        logger.info(f'gnoi-rpc baseline before the File RPCs: {baseline}')

        # --- Step 5: File.Put ---
        with allure.step(f'Step 5: File.Put with permission {GnoiFileService.PUT_MODE}'):
            out, err = client.file_put(local_file, put_on_dut, permission=GnoiFileService.PUT_MODE)
            assert not gnoi_rpc_failed(out, err), f'File.Put failed.\nout: {out}\nerr: {err}'
            mode, owner, size = dut_file_attributes(dut, put_on_dut)
            assert mode == str(GnoiFileService.PUT_MODE), \
                f'{put_on_dut} has mode {mode}, expected {GnoiFileService.PUT_MODE}'
            assert size == len(FILE_CONTENT), \
                f'{put_on_dut} is {size} bytes, expected {len(FILE_CONTENT)}'
            # The server resolves the requesting user through nv-user-lookupd and chowns to it.
            assert owner == username, (f'{put_on_dut} is owned by {owner}, but File.Put runs as the '
                                       f'requesting user {username}')

        # --- Step 6: File.Stat ---
        with allure.step('Step 6: File.Stat reports the path and size'):
            out, err = client.file_stat(put_on_dut)
            assert not gnoi_rpc_failed(out, err), f'File.Stat failed.\nout: {out}\nerr: {err}'
            assert put_on_dut in out, f'File.Stat did not report {put_on_dut}:\n{out}'
            assert str(len(FILE_CONTENT)) in out, \
                f'File.Stat did not report the {len(FILE_CONTENT)}-byte size:\n{out}'

        # --- Step 7: File.Get ---
        with allure.step('Step 7: File.Get returns the bytes intact'):
            get_dir = os.path.join(local_dir, 'get')
            os.makedirs(get_dir, exist_ok=True)
            out, err = client.file_get(put_on_dut, get_dir)
            assert not gnoi_rpc_failed(out, err), f'File.Get failed.\nout: {out}\nerr: {err}'
            retrieved = single_retrieved_file(get_dir)
            assert local_file_md5(retrieved) == dut_file_md5(dut, put_on_dut), (
                f'{retrieved} does not match {put_on_dut} on the switch')

        # --- Step 8: File.Put inheriting the local mode ---
        with allure.step(f'Step 8: File.Put with permission '
                         f'{GnoiFileService.PUT_MODE_INHERIT_LOCAL} inherits the local mode'):
            out, err = client.file_put(local_file, inherited_on_dut,
                                       permission=GnoiFileService.PUT_MODE_INHERIT_LOCAL)
            assert not gnoi_rpc_failed(out, err), \
                f'File.Put with permission 0 failed.\nout: {out}\nerr: {err}'
            mode, _, _ = dut_file_attributes(dut, inherited_on_dut)
            assert mode == local_mode, (f'{inherited_on_dut} has mode {mode}, but the local file is '
                                        f'{local_mode} and permission 0 means "inherit it"')

        # --- Step 9: Staging cleanup ---
        with allure.step('Step 9: the staging area holds nothing once the transfers are done'):
            verify_staging_area_clean(dut, 'after the put/get transfers')

        # --- Step 10: Tech-support dump retrieval ---
        with allure.step('Step 10: a tech-support dump can be stat-ed and retrieved intact'):
            generated_dump, duration = system.techsupport.action_generate(dut, test_name=test_name)
            logger.info(f'tech-support {generated_dump} generated in {duration}s')

            out, err = client.file_stat(generated_dump)
            assert not gnoi_rpc_failed(out, err), \
                f'File.Stat of the dump failed.\nout: {out}\nerr: {err}'
            assert generated_dump in out, f'File.Stat did not report {generated_dump}:\n{out}'

            dump_dir = os.path.join(local_dir, 'dump')
            os.makedirs(dump_dir, exist_ok=True)
            out, err = client.file_get(generated_dump, dump_dir,
                                       cmd_time=GNOIC_TRANSFER_TIMEOUT_SEC)
            assert not gnoi_rpc_failed(out, err), \
                f'File.Get of the dump failed.\nout: {out}\nerr: {err}'
            assert local_file_md5(single_retrieved_file(dump_dir)) == \
                dut_file_md5(dut, generated_dump), \
                'the retrieved tech-support dump does not match the one on the switch'

        # --- Step 11: File.Remove in a root-owned directory ---
        with allure.step(f'Step 11: File.Remove inside {GnoiFileService.TECHSUPPORT_DUMP_DIR} is '
                         'refused'):
            out, err = client.file_remove(generated_dump)
            assert gnoi_rpc_failed(out, err), (
                f'File.Remove deleted a dump from root-owned '
                f'{GnoiFileService.TECHSUPPORT_DUMP_DIR} while running as {username}.\n'
                f'out: {out}\nerr: {err}')
            assert file_exists_on_dut(dut, generated_dump), \
                f'{generated_dump} was removed even though the RPC reported an error'
            # On juliet-160 this comes back as Internal with 'File Not Found or Permission Denied',
            # which merges two conditions gRPC keeps apart and leaves the client unable to tell an
            # authorization refusal from a missing path.
            verify_refusal_status(out, err, GnoicErr.PERMISSION_DENIED,
                                  'removing a file from a directory the user cannot write')

        # --- Step 12: File.Remove of the user's own file ---
        with allure.step('Step 12: File.Remove deletes the user\'s own file'):
            out, err = client.file_remove(put_on_dut)
            assert not gnoi_rpc_failed(out, err), f'File.Remove failed.\nout: {out}\nerr: {err}'
            assert not file_exists_on_dut(dut, put_on_dut), \
                f'{put_on_dut} still exists after File.Remove'
            out, err = client.file_stat(put_on_dut)
            assert gnoi_rpc_failed(out, err), \
                f'File.Stat succeeded for a removed file.\nout: {out}\nerr: {err}'
            verify_refusal_status(out, err, GnoicErr.NOT_FOUND, 'stat of a removed file')

        # --- Step 13: Counters ---
        with allure.step('Step 13: received counters moved for the four File RPCs'):
            verify_rpc_counters_increased(
                gnoi_rpc, baseline,
                [GnoiRpc.FILE_PUT, GnoiRpc.FILE_STAT, GnoiRpc.FILE_GET, GnoiRpc.FILE_REMOVE])

    finally:
        with allure.step('Cleanup: remove the files this test created'):
            leftovers = ' '.join(dut_files + ([generated_dump] if generated_dump else []))
            dut.run_cmd(f'sudo rm -f {leftovers}')
            shutil.rmtree(local_dir, ignore_errors=True)
