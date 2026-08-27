import os
import time
import logging
import os
import shutil
import tempfile
import allure
import pexpect
import re
from retry.api import retry_call

from devts.infra.tools.connection_tools.onie_engine import OnieEngine
from devts.infra.tools.general_constants.air_constants import NvidiaAirConstants
from ngts.constants.constants import InfraConst, SonicDeployConstants, MarsConstants
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.helpers.json_file_helper import extract_fw_data, normalize_firmware_chassis_name
from ngts.helpers.run_process_on_host import run_process_on_host
from ngts.helpers.repack_sonic_bin_helper import (
    DEFAULT_REPACK_FROM_IMAGE,
    DEFAULT_REPACK_STAGING_DIR,
    get_bin_grub_sbat_gen,
    is_repacked_image_path,
    needs_repack_for_onie_install,
    parse_sbat_gen,
    repack_sonic_bin,
    resolve_local_image_path,
)

logger = logging.getLogger()

BASE_ONIE_UPDATER_URL = InfraConst.HTTTP_SERVER_FIT16 + '/onie_only/{}/9600/onie-updater-x86_64-mlnx_x86-r0'
DEFAULT_PROMPT = ["ONIE:.+ #", pexpect.EOF]

INSTALL_SUCCESS_PROMPT = r'Installed.*base\s+image.*successfully'
# mkfs.ext4 asks this when the SONiC-OS partition already has a filesystem;
# only the direct-sh install path hits it (onie-nos-install wipes the partition).
MKFS_PROCEED_PROMPT = r'Proceed anyway\? \(y,N\)'

SH_INSTALL_EXPECT_TIMEOUT = 300
SH_INSTALL_MAX_SILENT_PERIODS = 6
SH_INSTALL_MAX_PROCEED_ANSWERS = 3


class OnieInstallationError(Exception):
    """
    An exception for errors that reflect problematic behavior of the OS installation
    """


class SonicOnieCli:
    """
    This class defines methods over Onie
    """

    def __init__(self, host_ip, ssh_port=22, fw_pkg_path=None, platform_params=None):
        self.ip = host_ip
        self.ssh_port = ssh_port
        self.engine = None
        self.latest_onie_version = ''
        self.latest_onie_url = ''
        self.is_nos_installed = False
        self.fw_pkg_path = fw_pkg_path
        self.platform_params = platform_params
        self.create_engine()

    def create_engine(self, create_force=False):
        if self.engine is None or create_force:
            self.engine = OnieEngine(self.ip, 'root', self.ssh_port).create_engine()
            retry_call(self.get_pexpect_entry,
                       fargs=[['#'], 10],
                       tries=3,
                       delay=5,
                       logger=logger)
            self.engine.timeout = 10

    def run_cmd_set(self, cmd_list, custom_prompts="", timeout=None):
        prompts = custom_prompts if custom_prompts else DEFAULT_PROMPT
        stdout = ""
        pexpect_entry = ""
        for cmd in cmd_list:
            logger.info(f'Executing command on {self.ip}: {cmd}')
            self.engine.sendline(cmd)
            pexpect_entry = self.get_pexpect_entry(prompts, timeout)
            stdout = self.get_stdout(stdout)
        return stdout, pexpect_entry

    def get_pexpect_entry(self, prompts, timeout=None):
        return self.engine.expect(prompts, timeout=timeout)

    def get_stdout(self, stdout=""):
        stdout += self.engine.before.decode('ascii')
        logger.info("stdout: " + stdout)
        return stdout

    def confirm_onie_boot_mode_install(self):
        self.confirm_onie_boot_mode('install')

    def confirm_onie_boot_mode_update(self):
        self.confirm_onie_boot_mode('update')

    def confirm_onie_boot_mode(self, mode):
        boot_info_output = self.get_boot_info()
        if f'boot_reason={mode}' not in boot_info_output:
            logger.info(f'Changing the ONIE boot mode to {mode}')
            self.is_nos_installed, _ = self.run_cmd_set(['ls /dev/sda3'])
            if self.is_efi_system():
                cmds_list = ['mkdir -p /mnt/onie-boot',
                             'mount LABEL="ONIE-BOOT" /mnt/onie-boot']
                self.run_cmd_set(cmds_list)
                self.set_efi_next_boot_onie()
                set_onie_mode_output, _ = self.run_cmd_set([f"grub-editenv /mnt/onie-boot/grub/grubenv set onie_mode={mode}"])
                if "grub-editenv: error" in set_onie_mode_output:
                    self.manual_set_onie_mode_efi_system(mode)
            else:
                if self.is_nos_installed:
                    # If NOS installed - set GRUB force boot into ONIE
                    cmds_list = ['mkdir /boot',
                                 'mount /dev/sda3  /boot/',
                                 'grub-editenv /boot/grub/grubenv set next_entry=ONIE',
                                 'mount LABEL=ONIE-BOOT /mnt/onie-boot',
                                 'onie-nos-mode -c -v']
                    self.run_cmd_set(cmds_list)
                self.run_cmd_set([f'onie-boot-mode -o {mode}'])
            self.reboot()
        else:
            logger.info(f'Switch is in ONIE {mode} mode')

    def get_boot_info(self):
        get_boot_info_cmd = 'cat /proc/cmdline'
        # right after reboot, we can get the empty output from cmd run.
        for _ in range(3):
            boot_info_output, _ = self.run_cmd_set([get_boot_info_cmd])
            if 'boot_reason' in boot_info_output:
                break
            time.sleep(1)
        return boot_info_output

    def is_efi_system(self):
        # right after reboot, we can get the empty output from cmd run.
        for _ in range(3):
            efibootmgr_output, _ = self.run_cmd_set(["efibootmgr"])
            if 'BootOrder' in efibootmgr_output:
                return True
            if 'EFI variables are not supported' in efibootmgr_output:
                return False
            time.sleep(1)
        raise AssertionError(f'Can not detect if system is EFI or not.\nefibootmgr output:\n{efibootmgr_output}')

    def set_efi_next_boot_onie(self):
        logger.info('in set efi next boot')
        boot_line_pattern = re.compile(r'^Boot(\d+)\* ONIE.*$', re.IGNORECASE | re.MULTILINE)
        for _ in range(3):
            efibootmgr_output, _ = self.run_cmd_set(['efibootmgr'], custom_prompts=["ONIE:~ #", pexpect.EOF])
            match = boot_line_pattern.search(efibootmgr_output)
            if match:
                onie_boot_num = match.group(1)
                self.run_cmd_set([f'efibootmgr -n {onie_boot_num}'])
                return
            time.sleep(1)
        raise AssertionError(f'Can not parse efibootmgr output:\n{efibootmgr_output}')

    def manual_set_onie_mode_efi_system(self, mode):
        # This flow restoring the grub config
        logger.warning(f'Failed to set onie_mode {mode} in /mnt/onie-boot/grub/grubenv.'
                       f'\n Will use manual set of onie_mode')
        self.run_cmd_set([f'if ! grep -q "onie_mode=" /mnt/onie-boot/grub/grubenv;'
                          f' then echo "onie_mode={mode}" >> /mnt/onie-boot/grub/grubenv; '
                          f'else sed -i "s/onie_mode=.*/onie_mode={mode}/" /mnt/onie-boot/grub/grubenv; '
                          f'fi'])

    def reboot(self):
        self.run_cmd_set(['reboot'])
        self.post_reboot_delay()

    def post_reboot_delay(self):
        check_port_status_till_alive(should_be_alive=False, destination_host=self.ip, destination_port=self.ssh_port,
                                     tries=30)
        check_port_status_till_alive(should_be_alive=True, destination_host=self.ip, destination_port=self.ssh_port)
        logger.info(
            f'Sleeping {InfraConst.SLEEP_AFTER_RRBOOT} seconds after switch reply to ping to handle ssh session')
        time.sleep(InfraConst.SLEEP_AFTER_RRBOOT)

    def _onie_run_and_capture(self, cmd):
        """Run a command on ONIE and return its output.

        Send ``cmd`` and a unique sentinel together. If the session was still
        one command behind (sentinel not yet echoed), drain with a second echo.
        The sentinel must be unique per call: a fixed one would also match the
        stale buffer left by the previous call's drain echo, so we would return
        that stale output instead of this command's.
        """
        self._onie_cmd_seq = getattr(self, '_onie_cmd_seq', 0) + 1
        sentinel = f'ONIE_CMD_DONE_{self._onie_cmd_seq}'
        out, _ = self.run_cmd_set([f'{cmd}; echo {sentinel}'])
        if sentinel not in out:
            out, _ = self.run_cmd_set([f'echo {sentinel}'])
        return out

    def _onie_file_exists(self, path):
        marker = 'ONIE_FILE_EXISTS'
        out = self._onie_run_and_capture(f'test -f {path} && echo {marker} || true')
        return marker in out

    def ensure_esp_mounted(self):
        """Mount the ESP if needed and return the SONiC EFI dir, or None."""
        check_paths = [
            '/boot/efi/EFI/SONiC-OS/grubx64.efi',
            '/UEFI/EFI/SONiC-OS/grubx64.efi',
        ]
        for path in check_paths:
            if self._onie_file_exists(path):
                logger.info(f'ensure_esp_mounted: found ESP grub at {path}')
                return os.path.dirname(path)

        mount_point = '/boot/efi'
        self.run_cmd_set([f'mkdir -p {mount_point}'])
        grub_path = f'{mount_point}/EFI/SONiC-OS/grubx64.efi'
        for mount_cmd in (
            f'mount LABEL=ESP {mount_point}',
            f'mount LABEL=EFI {mount_point}',
            f'mount LABEL="EFI_SYSTEM" {mount_point}',
        ):
            self.run_cmd_set([f'{mount_cmd} || true'])
        if self._onie_file_exists(grub_path):
            return f'{mount_point}/EFI/SONiC-OS'

        part_out = self._onie_run_and_capture(
            'fdisk -l 2>/dev/null | grep "EFI System" | awk \'{print $1}\' | head -n1 || true')
        part_match = re.search(r'(/dev/\S+)', part_out)
        if part_match:
            part = part_match.group(1)
            self.run_cmd_set([f'mount {part} {mount_point} || true'])
            if self._onie_file_exists(grub_path):
                return f'{mount_point}/EFI/SONiC-OS'
        return None

    def get_esp_sbat_gen(self, component='grub', efi_dir=None):
        """Return SBAT generation for grub/shim on the DUT ESP, or None."""
        efi_dir = efi_dir or self.ensure_esp_mounted()
        if not efi_dir:
            logger.info('get_esp_sbat_gen: SONiC ESP grub/shim not available')
            return None
        efi_path = f'{efi_dir}/{component}x64.efi'
        if not self._onie_file_exists(efi_path):
            logger.warning(f'get_esp_sbat_gen: missing EFI binary at {efi_path}')
            return None
        # BusyBox grep may lack -a; fall back to stripping non-ascii.
        out = self._onie_run_and_capture(
            f'(grep -a -o -m1 "{component},[0-9]*" {efi_path} || '
            f'cat {efi_path} | tr -cd "\\11\\12\\15\\40-\\176" | '
            f'grep -o -m1 "{component},[0-9]*" || true)'
        )
        gen = parse_sbat_gen(out.encode('latin1', errors='replace'), component)
        logger.info(f'get_esp_sbat_gen: {component} gen={gen} from {efi_path}')
        return gen

    def prepare_image_for_onie_install(self, image_path, device_grub_gen):
        """Return (image_path, use_direct_sh, cleanup_dir) after optional SBAT repack."""
        local_path = resolve_local_image_path(image_path)
        if is_repacked_image_path(local_path):
            return local_path, True, None

        if not needs_repack_for_onie_install(device_grub_gen, None):
            return local_path, False, None

        if not os.path.isfile(local_path):
            raise FileNotFoundError(
                f'Candidate image is not available locally for SBAT inspection/repack: {local_path}'
            )

        image_grub_gen = get_bin_grub_sbat_gen(local_path)
        logger.info(
            f'ONIE install SBAT: DUT grub,{device_grub_gen}; '
            f'candidate grub,{image_grub_gen}'
        )
        if not needs_repack_for_onie_install(device_grub_gen, image_grub_gen):
            return local_path, False, None

        from_image = DEFAULT_REPACK_FROM_IMAGE
        logger.info(f'Using default repack --from-image: {from_image}')
        if not os.path.isfile(from_image):
            raise FileNotFoundError(
                f'Default --from-image is not available locally: {from_image}'
            )

        os.makedirs(DEFAULT_REPACK_STAGING_DIR, exist_ok=True)
        # Explicit chmod: makedirs mode is umask-masked / ignored if dir exists.
        # 1777 is required under NFS root-squash (Mars runs as root → nobody).
        try:
            os.chmod(DEFAULT_REPACK_STAGING_DIR, 0o1777)
        except OSError as exc:
            logger.info(f'Could not chmod {DEFAULT_REPACK_STAGING_DIR}: {exc}')
        if not os.access(DEFAULT_REPACK_STAGING_DIR, os.W_OK | os.X_OK):
            raise PermissionError(
                f'Repack staging dir is not writable: {DEFAULT_REPACK_STAGING_DIR}. '
                f'It needs mode 1777 so any mgmt identity can stage images'
            )

        work_dir = tempfile.mkdtemp(
            prefix=f'sonic-repack-{self.ip.replace(".", "-")}-',
            dir=DEFAULT_REPACK_STAGING_DIR,
        )
        os.chmod(work_dir, 0o755)
        try:
            updated_image = repack_sonic_bin(local_path, from_image, work_dir)
            os.chmod(updated_image, 0o644)
            logger.info(f'Repacked image staged for HTTP/wget: {updated_image}')
            return updated_image, True, work_dir
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

    def _wait_install_success(self, stdout, pexpect_entry, prompts, timeout, num_retry):
        max_retry = num_retry
        while num_retry > 0:
            if pexpect_entry == 0:
                break
            elif pexpect_entry == 1:
                num_retry = num_retry - 1
                pexpect_entry = self.get_pexpect_entry(prompts, timeout)
                stdout = self.get_stdout(stdout)
                logger.info(f"Got timeout on {num_retry} time..")
            else:
                logger.info(f'Caught pexpect entry: {pexpect_entry}')
                raise OnieInstallationError(f"Failed to install sonic image. {stdout}")
        else:
            logger.info(
                f"Did not installed image in {max_retry} attempts of {timeout} seconds.")
            raise OnieInstallationError(f"Failed to install sonic image. {stdout}")

    def _run_sh_install(self, local_image_file):
        """Run ``sh <image>``, answering mkfs ``Proceed anyway?`` if it appears.

        onie-nos-install never hits this prompt because it wipes the partition
        first; the repacked/SBAT path does.
        """
        expect_prompts = [INSTALL_SUCCESS_PROMPT, MKFS_PROCEED_PROMPT, pexpect.TIMEOUT]
        logger.info(f'Executing command on {self.ip}: sh {local_image_file}')
        self.engine.sendline(f'sh {local_image_file}')

        stdout = ""
        silent_periods = SH_INSTALL_MAX_SILENT_PERIODS
        proceed_answers = SH_INSTALL_MAX_PROCEED_ANSWERS
        while silent_periods > 0:
            pexpect_entry = self.get_pexpect_entry(expect_prompts, SH_INSTALL_EXPECT_TIMEOUT)
            stdout = self.get_stdout(stdout)
            if pexpect_entry == 0:
                return stdout
            if pexpect_entry == 1:
                proceed_answers -= 1
                if proceed_answers < 0:
                    raise OnieInstallationError(
                        f"mkfs kept asking 'Proceed anyway?', 'y' is not consumed. {stdout}")
                logger.info("mkfs 'Proceed anyway?' prompt detected; answering 'y'")
                self.engine.sendline('y')
                continue
            silent_periods -= 1
            logger.info(f'No install output for {SH_INSTALL_EXPECT_TIMEOUT}s, '
                        f'{silent_periods} periods left')
        raise OnieInstallationError(f"Failed to install sonic image. {stdout}")

    def _get_device_grub_sbat_gen(self):
        """Return DUT ESP grub SBAT generation, or None if unavailable."""
        try:
            if not self.is_efi_system():
                return None
            efi_dir = self.ensure_esp_mounted()
            if not efi_dir:
                return None
            return self.get_esp_sbat_gen('grub', efi_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'Unable to determine DUT grub SBAT gen: {exc}')
            return None

    def _wget_image_to_onie(self, image_url, local_image_file, timeout):
        """Fetch an image onto ONIE over HTTP (expects wget ``100%``)."""
        logger.info(f'Downloading image to ONIE via HTTP: {image_url} -> {local_image_file}')
        download_image_cmd = f'wget --timeout=310 --continue -O {local_image_file} {image_url}'
        retry_call(self.run_cmd_set, fargs=[[download_image_cmd]],
                   fkwargs={'custom_prompts': '100%', 'timeout': timeout},
                   tries=5, delay=2, logger=logger)

    def _install_image_via_sh(self, install_src, local_image_file, cleanup_dir=None):
        """Install a repacked image via wget + ``sh`` (CMS signature is invalid)."""
        local_src = resolve_local_image_path(install_src)
        if not local_src.startswith('/auto/'):
            raise AssertionError(
                f'Repacked image must be under /auto for HTTP/wget install, got: {local_src}'
            )
        try:
            with allure.step('Download updated image to ONIE via HTTP/wget'):
                self._wget_image_to_onie(
                    f'{MarsConstants.HTTP_SERVER_NBU_NFS}{local_src}', local_image_file, 900)

            logger.info(f'Starting direct sh install of {local_image_file}')
            with allure.step('Installing image by "sh" (repacked / SBAT-updated)'):
                self._run_sh_install(local_image_file)
                logger.info(
                    'SONiC installed via sh; rebooting (direct install does not auto-reboot)'
                )
                self.run_cmd_set(['reboot'])
        finally:
            if cleanup_dir:
                shutil.rmtree(cleanup_dir, ignore_errors=True)

    def _install_image_via_onie_nos_install(self, image_path, install_src, local_image_file,
                                            prompts, timeout, num_retry):
        full_image_path = f"{MarsConstants.HTTP_SERVER_NBU_NFS}{image_path}"
        resolved = resolve_local_image_path(install_src)
        if resolved.startswith('/auto/'):
            full_image_path = f"{MarsConstants.HTTP_SERVER_NBU_NFS}{resolved}"
            local_image_file = '/tmp/' + os.path.basename(resolved)
            self.run_cmd_set([f'rm -f "{local_image_file}"'])

        self._wget_image_to_onie(full_image_path, local_image_file, 300)

        logger.info('Starting onie-nos-install sonic image')
        with allure.step('Installing image by "onie-nos-install"'):
            stdout, pexpect_entry = self.run_cmd_set(
                [f"onie-nos-install {local_image_file}"], prompts, timeout=timeout)
            self._wait_install_success(stdout, pexpect_entry, prompts, timeout, num_retry)

    def install_image(self, image_path, timeout=60, num_retry=10):
        """Install a SONiC .bin from ONIE, repacking for SBAT when needed."""
        self.confirm_onie_boot_mode_install()
        # restore engine after reboot
        self.create_engine(True)

        device_grub_gen = self._get_device_grub_sbat_gen()

        with allure.step('Prepare image for ONIE install (SBAT / repack if needed)'):
            install_src, use_direct_sh, cleanup_dir = self.prepare_image_for_onie_install(
                image_path, device_grub_gen)

        local_image_file = '/tmp/' + os.path.basename(install_src)
        logger.info('Removing existing sonic image if any')
        self.run_cmd_set([f'rm -f "{local_image_file}"'])

        if use_direct_sh:
            self._install_image_via_sh(install_src, local_image_file, cleanup_dir)
        else:
            prompts = [INSTALL_SUCCESS_PROMPT, pexpect.TIMEOUT]
            self._install_image_via_onie_nos_install(
                image_path, install_src, local_image_file, prompts, timeout, num_retry)

        logger.info('SONiC installed')
        prompts = [pexpect.EOF, pexpect.TIMEOUT]
        self.get_pexpect_entry(prompts, timeout=15)
        stdout = self.get_stdout()
        return stdout

    def download_image(self, image_path, platform_params, topology_obj):
        """
        Download SONiC image into /tmp/sonic-mellanox.bin on DUT(ONIE)
        """
        logger.info(f'Getting SONiC image from: {image_path} using SCP')
        image_name = 'sonic-mellanox.bin'
        dst_image_file_path = f'/tmp/{image_name}'

        with allure.step('Downloading SONiC image'):
            if 'air' in platform_params.setup_name:
                self.download_image_into_air(topology_obj, image_path, image_name, dst_image_file_path)
                dst_image_file_path = f'http://{NvidiaAirConstants.NVIDIA_AIR_OOB_MGMT_SERVER_IP}/{image_name}'
            else:
                self.download_image_into_nvidia_lab(image_path, dst_image_file_path)

        return dst_image_file_path

    @staticmethod
    def download_image_into_air(topology_obj, image_path, image_name, dst_image_file_path):
        """
        Download SONiC image into NvidiaAir simulation
        First download image to oob-mgmt-server
        Second download image from oob-mgmt-server to DUT(ONIE)
        """
        # Copy to oob-mgmt-server
        oob_engine = topology_obj.players['oob-mgmt-server']['engine']
        oob_engine.copy_file(source_file=image_path, file_system='/tmp', dest_file=image_name)
        oob_engine.run_cmd(f'sudo mv {dst_image_file_path} /var/www/html/')

    def download_image_into_nvidia_lab(self, image_path, dst_image_file_path):
        """
        Download SONiC image using SCP directly to DUT(ONIE)
        """
        # Copy directly to DUT
        scp_cmd = f'sshpass -p \'root\' scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -P {self.ssh_port} ' \
            f'{image_path} root@{self.ip}:{dst_image_file_path}'
        out, err, rc = run_process_on_host(scp_cmd, timeout=1800)
        if rc:
            logger.error(f'Failed copy SONiC image, std_out: {out}, std_err: {err}, rc: {rc}')
            raise AssertionError('SCP copy file to DUT failed')

    def update_onie(self):
        if self.required_onie_installation():
            with allure.step(f"Install on {self.ip} required ONIE version {self.latest_onie_version}"):
                logger.info(
                    f'Switch {self.ip} have not required ONIE version {self.latest_onie_version}, ONIE will be updated')
                self.confirm_onie_boot_mode_update()
                # restore engine after reboot
                self.create_engine(True)
                if self.is_nos_installed:
                    if self.is_efi_system():
                        self.set_efi_next_boot_onie()
                        cmds_list = ['mkdir -p /mnt/onie-boot',
                                     'mount LABEL="ONIE-BOOT" /mnt/onie-boot']
                    else:
                        # If NOS installed - set GRUB force boot into ONIE
                        cmds_list = ['mkdir /boot',
                                     'mount /dev/sda3  /boot/',
                                     'grub-editenv /boot/grub/grubenv set next_entry=ONIE']
                    self.run_cmd_set(cmds_list)
                cmd_output, _ = self.run_cmd_set([f'onie-self-update {self.latest_onie_url}'])
                logger.info(f'onie-self-update command output:\n{cmd_output}')
                self.post_reboot_delay()
        else:
            with allure.step(f"Switch {self.ip} doesn't required ONIE installation"):
                logger.info(f"Switch {self.ip} doesn't required ONIE installation")

    def required_onie_installation(self):
        # right after reboot, we can get the empty output from cmd run.
        install_required = True
        for _ in range(3):
            onie_version_output, _ = self.run_cmd_set(['onie-sysinfo -v'])
            if '9600' in onie_version_output or '115200' in onie_version_output:
                break
            time.sleep(1)
        self.latest_onie_version, self.latest_onie_url = get_latest_onie_version(self.fw_pkg_path,
                                                                                 self.platform_params)
        host_name = self.platform_params.host_name
        # prod devices can't be updated by regular ONIE
        if self.latest_onie_version in onie_version_output or host_name in SonicDeployConstants.PRODUCTION_DUTS:
            install_required = False
        return install_required


def get_latest_onie_version(fw_pkg_path, platform_params):
    latest_onie_version = ''
    latest_onie_url = ''
    if fw_pkg_path is None:
        logger.warning("No firmware package file path specified, falling back to default.")
        fw_pkg_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)).split('ngts')[0],
            MarsConstants.UPDATED_FW_TAR_PATH
        )
    if fw_pkg_path:
        logger.info(f"Get latest ONIE version from specified file {fw_pkg_path}")
        fw_data = extract_fw_data(fw_pkg_path)
        chassis = normalize_firmware_chassis_name(platform_params.filtered_platform)
        if chassis in fw_data["chassis"]:
            component = fw_data["chassis"][chassis]["component"]
        else:
            component = {}
        hostname = platform_params.host_name
        if "host" in fw_data and hostname in fw_data["host"]:
            for component_type in list(fw_data["host"][hostname]["component"].keys()):
                component[component_type] = fw_data["host"][hostname]["component"][component_type]

        if "ONIE" in component:
            onie_info_list = component["ONIE"]
            for onie_version_info in onie_info_list:
                if latest_onie_version < onie_version_info["version"]:
                    latest_onie_version = onie_version_info["version"]
                    latest_onie_url = onie_version_info["firmware"]
        else:
            logger.warning(f"The specified platform {platform_params.filtered_platform.upper()} not in the"
                           f" provided firmware package file {fw_pkg_path}")
        logger.info(f"The latest ONIE version is {latest_onie_version}, the latest ONIE url is {latest_onie_url}")
    return latest_onie_version, latest_onie_url
