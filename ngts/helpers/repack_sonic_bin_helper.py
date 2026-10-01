"""
Helpers for SBAT-aware ONIE install of older SONiC images.

Trixie GRUB ratchets UEFI SBAT to grub,5+, so older images (grub,4) fail to
boot after a normal ONIE install. repack-sonic-bin.sh injects newer
grub/shim/mm; the result must be installed with ``sh`` (not onie-nos-install)
because repacking invalidates the CMS signature.

See: https://nvidia.atlassian.net/wiki/spaces/SW/pages/3759951082
"""

import logging
import os
import re
import shlex
import shutil
import tarfile
import tempfile
import zipfile

from ngts.helpers.run_process_on_host import run_process_on_host

logger = logging.getLogger()

# Generation that Trixie GRUB / current shim require.
SBAT_GRUB_GEN_TRIXIE = 5

# Mounted in Mars sonic-mgmt containers (unlike /auto/mtrsysgwork).
DEFAULT_REPACK_SCRIPT = '/auto/sw_system_project/sonic/bin_repack/repack-sonic-bin.sh'
DEFAULT_REPACK_FROM_IMAGE = '/auto/sw_system_project/sonic/bin_repack/sonic-mellanox.bin'
# HTTP-served via MarsConstants.HTTP_SERVER_NBU_NFS for ONIE wget.
DEFAULT_REPACK_STAGING_DIR = '/auto/sw_system_project/sonic/bin_repack/tmp'

ENV_REPACK_SCRIPT = 'SONIC_REPACK_SCRIPT'

_UPDATED_SUFFIX = '-updated.bin'
_EXIT_MARKER = b'exit_marker\n'
_GRUB_EFI_CANDIDATES = (
    'boot/grubx64.efi',
    'grubx64.efi',
)


def resolve_local_image_path(image_path):
    """Normalize an HTTP /auto URL to a local filesystem path when possible."""
    if image_path and image_path.startswith(('http://', 'https://')) and '/auto/' in image_path:
        return '/auto/' + image_path.split('/auto/', 1)[1]
    return image_path


def is_repacked_image_path(image_path):
    return os.path.basename(image_path or '').endswith(_UPDATED_SUFFIX)


def parse_sbat_gen(data, component='grub'):
    """Return SBAT generation for component inside an EFI binary, or None."""
    if not data:
        return None
    match = re.search(rf'{re.escape(component)},([0-9]+)'.encode(), data)
    if not match:
        return None
    return int(match.group(1))


def needs_repack_for_onie_install(device_grub_gen, image_grub_gen):
    """True when DUT requires grub,5+ and the candidate image is older/unknown."""
    if device_grub_gen is None or device_grub_gen < SBAT_GRUB_GEN_TRIXIE:
        return False
    if image_grub_gen is not None and image_grub_gen >= SBAT_GRUB_GEN_TRIXIE:
        return False
    return True


def get_repack_script_path():
    return os.environ.get(ENV_REPACK_SCRIPT, DEFAULT_REPACK_SCRIPT)


def get_bin_grub_sbat_gen(bin_path):
    """Return grub SBAT gen from a SONiC .bin (fs.zip → grubx64.efi), or None."""
    if not bin_path or not os.path.isfile(bin_path):
        logger.warning(f'get_bin_grub_sbat_gen: not a file: {bin_path}')
        return None

    try:
        with open(bin_path, 'rb') as bin_fh:
            header = bin_fh.read(65536)
            marker_off = header.find(_EXIT_MARKER)
            if marker_off < 0:
                logger.warning(f'get_bin_grub_sbat_gen: no exit_marker in {bin_path}')
                return None
            header_len = marker_off + len(_EXIT_MARKER)
            header_text = header[:header_len].decode('latin1', errors='replace')
            size_match = re.search(r'^payload_image_size=([0-9]+)', header_text, re.M)
            if not size_match:
                logger.warning(f'get_bin_grub_sbat_gen: no payload_image_size in {bin_path}')
                return None
            payload_size = int(size_match.group(1))

            class _LimitedReader:
                def __init__(self, fh, nbytes):
                    self._fh = fh
                    self._left = nbytes

                def read(self, size=-1):
                    if self._left <= 0:
                        return b''
                    if size < 0 or size > self._left:
                        size = self._left
                    chunk = self._fh.read(size)
                    self._left -= len(chunk)
                    return chunk

            bin_fh.seek(header_len)
            with tarfile.open(fileobj=_LimitedReader(bin_fh, payload_size), mode='r|') as tar:
                for member in tar:
                    name = member.name
                    if not (name.endswith('fs.zip') or name.endswith('/fs.zip')):
                        continue
                    src = tar.extractfile(member)
                    if src is None:
                        return None
                    with tempfile.NamedTemporaryFile(prefix='sonic_fs_', suffix='.zip') as tmp:
                        shutil.copyfileobj(src, tmp, length=1024 * 1024)
                        tmp.flush()
                        with zipfile.ZipFile(tmp.name) as zf:
                            for efi_name in _GRUB_EFI_CANDIDATES:
                                try:
                                    return parse_sbat_gen(zf.read(efi_name), 'grub')
                                except KeyError:
                                    continue
                            for zip_name in zf.namelist():
                                if zip_name.lower().endswith('grubx64.efi'):
                                    return parse_sbat_gen(zf.read(zip_name), 'grub')
                    return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f'get_bin_grub_sbat_gen: failed for {bin_path}: {exc}')
        return None

    logger.info(f'get_bin_grub_sbat_gen: no grubx64.efi in fs.zip of {bin_path}')
    return None


def repack_sonic_bin(image_path, from_image, output_dir):
    """
    Repack an older SONiC image on the mgmt host.

    grub/shim/mm come as a matched set from from_image. Output is written only
    under output_dir; neither source image is modified.
    """
    image_path = os.path.realpath(image_path)
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f'Candidate image not found: {image_path}')

    from_image = os.path.realpath(from_image)
    if not os.path.isfile(from_image):
        raise FileNotFoundError(f'Newer --from-image not found: {from_image}')

    script = get_repack_script_path()
    if not os.path.isfile(script):
        raise FileNotFoundError(
            f'repack script not found: {script}. Set {ENV_REPACK_SCRIPT} if needed'
        )

    if not os.path.isdir(output_dir):
        raise FileNotFoundError(f'Repack output directory not found: {output_dir}')
    image_name = os.path.basename(image_path)
    base, ext = os.path.splitext(image_name)
    output_path = os.path.join(output_dir, f'{base}-updated{ext or ".bin"}')

    # Keep scratch on local disk: NFS root-squash rejects tar ownership changes,
    # and local I/O is much faster for a ~1.4GB unpack.
    work_parent = tempfile.mkdtemp(prefix='sonic-repack-work-')
    # run_process_on_host() uses shlex.split() on the string.
    cmd = (
        f'{shlex.quote(script)} -i {shlex.quote(image_path)} '
        f'--from-image {shlex.quote(from_image)} '
        f'--workdir {shlex.quote(work_parent)} '
        f'-o {shlex.quote(output_path)}'
    )
    logger.info(f'Repacking SONiC image for SBAT-compatible ONIE install: {cmd}')
    try:
        stdout, stderr, rc = run_process_on_host(cmd, timeout=1800)
    finally:
        shutil.rmtree(work_parent, ignore_errors=True)
    if isinstance(stdout, bytes):
        stdout = stdout.decode('utf-8', errors='replace')
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', errors='replace')
    if rc != 0:
        raise RuntimeError(
            f'repack-sonic-bin failed (rc={rc}). stdout:\n{stdout}\nstderr:\n{stderr}'
        )
    logger.info(f'repack-sonic-bin stdout:\n{stdout}')
    if stderr:
        logger.info(f'repack-sonic-bin stderr:\n{stderr}')

    if not os.path.isfile(output_path):
        raise FileNotFoundError(f'repack did not produce output: {output_path}')

    return output_path
