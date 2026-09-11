import os
import re
import string
import random
import pytest
import logging

from retry import retry
from retry.api import retry_call
from tests.common.utilities import wait_until
from ngts.constants.constants import MarsConstants
from ngts.helpers.json_file_helper import extract_fw_data, normalize_firmware_chassis_name
from ngts.cli_util.cli_parsers import generic_sonic_output_parser
from devts.infra.tools.general_constants.constants import DockerBringupConstants
from devts.infra.tools.general_constants.constants import DefaultConnectionValues
from tests.common.plugins.allure_wrapper import allure_step_wrapper as allure
from devts.infra.tools.validations.traffic_validations.ping.send import ping_till_alive
# import via the factory module: in the source layout the direct module path loads a second
# copy of the class, breaking isinstance() against factory-created engines
from devts.infra.tools.connection_tools.serial_engine_factory import (
    PexpectSerialEngine,
)
from devts.infra.tools.connection_tools.utils import extend_rcon_with_ssh_options
from ngts.cli_wrappers.sonic.sonic_onie_clis import SonicOnieCli
from ngts.tests.nightly.secure.constants import SecureBootConsts, SonicSecureBootConsts, SecureUpgradeConsts

logger = logging.getLogger()
allure.logger = logger


class SecureBootHelper:

    def __init__(self, serial_engine, engines, cli_objects):
        self.serial_engine = serial_engine
        self.engines = engines
        self.cli_objects = cli_objects

    @staticmethod
    def pytest_addoption(parser):
        parser.addoption("--restore_to_image",
                         action="store", required=False, default=None, help="restore SONiC image after error flow")

    @staticmethod
    def get_serial_engine(topology_obj):
        serial_engine = SecureBootHelper.get_serial_engine_instance(topology_obj)
        serial_engine.create_serial_engine()
        return serial_engine

    @staticmethod
    def get_serial_engine_instance(topology_obj, dut_alias='dut'):
        serial_alias = dut_alias + '_serial'
        logger.info("Serial engine is: {}".format(serial_alias))
        att = topology_obj.players[serial_alias]['attributes'].noga_query_data['attributes']
        # built directly (not via SerialEngineFactory.create) because secure-boot
        # needs a pexpect engine even on non-NVUE (SONiC) setups
        return PexpectSerialEngine(
            ip=att['Specific']['ip'],
            username=att['Topology Conn.']['CONN_USER'],
            password=att['Topology Conn.']['CONN_PASSWORD'],
            rcon_command=extend_rcon_with_ssh_options(att['Specific']['serial_conn_cmd']),
            timeout=30)

    @staticmethod
    def get_non_secure_image_path():
        return SecureBootConsts.NON_SECURE_IMAGE_PATH

    @staticmethod
    def get_sig_mismatch_image_path():
        return SecureBootConsts.SIG_MISMATCH_IMAGE_PATH

    @staticmethod
    def get_restore_to_image_path(request):
        restore_to_image = request.config.getoption('restore_to_image')
        if ',' in restore_to_image:
            base_version, target_version = restore_to_image.split(',')
            logger.info(f"base_version: {base_version}, target_version: {target_version}")
            return target_version if target_version else base_version
        logger.info(f"restore_to_image: {restore_to_image}")
        return restore_to_image

    def get_test_server_engine(self):
        player = self.engines['sonic_mgmt']
        return player

    def get_vmiluz_file_path(self):
        output = self.engines.dut.run_cmd('ls {}'.format(SecureBootConsts.VMILUNZ_DIR))
        path = re.findall(SecureBootConsts.VMILUNZ_REGEX, output)[0]
        return SecureBootConsts.VMILUNZ_DIR + path

    def mount_uefi_disk_partition(self):
        logger.info("mounting UEFI disk partition at {}".format(SecureBootConsts.MOUNT_FOLDER))
        self.serial_engine.run_cmd(SecureBootConsts.ROOT_PRIVILAGE)
        self.serial_engine.run_cmd("mkdir {}".format(SecureBootConsts.MOUNT_FOLDER))
        output = self.serial_engine.run_cmd(SecureBootConsts.EFI_PARTITION_CMD,
                                            SecureBootConsts.LAST_OCCURENCE_REGEX.format('#'))[0]
        uefi_partitions = re.findall('\\/dev\\/sda\\d', output)
        if not uefi_partitions:
            uefi_partitions = re.findall('\\/dev\\/nvme\\S+', output)
        assert uefi_partitions, "No UEFI partition found"
        uefi_partition = uefi_partitions[0]
        self.serial_engine.run_cmd("mount -o rw,auto,user,fmask=0022,dmask=0000 {} {}".
                                   format(uefi_partition, SecureBootConsts.MOUNT_FOLDER))


class SonicSecureBootHelper(SecureBootHelper):

    def is_sonic_mode(self):
        try:
            _, respond = self.serial_engine.run_cmd('\r', ["/home/admin#",
                                                           DefaultConnectionValues.DEFAULT_USER + '@',
                                                           "Debian GNU/Linux.*",
                                                           "Please press Enter to activate this console",
                                                           DefaultConnectionValues.LOGIN_REGEX,
                                                           rf"{DefaultConnectionValues.DEFAULT_USER}@[^\r\n]*:.*[$#]",
                                                           "Malformed binary after Attribute Certificate Table",
                                                           "GNU GRUB  version"],
                                                    timeout=SonicSecureBootConsts.ONIE_TIMEOUT)
            if respond in [0, 1, 2, 5]:
                logger.info("SONIC mode")
                return True
            else:
                logger.info("Not in SONIC mode")
                return False
        except Exception as e:
            logger.warning(f"Failed to check SONIC mode via serial command: {str(e)}")
            return False

    def get_backup_vmlinuz_signature(self):
        output = self.serial_engine.run_cmd_and_get_output(
            'ls {} | grep {}'.format(SecureBootConsts.TMP_FOLDER, SonicSecureBootConsts.ORIGIN_TAG))
        backup_signature_name = re.findall(SecureBootConsts.VMILUNZ_REGEX, output)[0]
        return backup_signature_name

    def restore_vmlinuz_signature(self):
        if self.is_sonic_mode():
            logger.info("Try to do a run time recovery for vmlinuz under fast and warm reboot")
            backup_signature_name = self.get_backup_vmlinuz_signature()
            self.serial_engine.run_cmd('cp -f {}/{} {}{}'.
                                       format(SecureBootConsts.TMP_FOLDER,
                                              backup_signature_name + SonicSecureBootConsts.ORIGIN_TAG,
                                              SecureBootConsts.VMILUNZ_DIR,
                                              backup_signature_name))
            return True

    def get_image_version(self):
        output = self.engines.dut.run_cmd('sudo show boot')
        current_image = re.search(r"Current:\s*(SONiC-OS-[\d|\w|\-]*\..*)", output, re.IGNORECASE).group(1)
        return current_image

    def set_default_image(self, image):
        self.cli_objects.dut.general.set_default_image(image)

    def validate_secure_upgrade_sonic_installer(self, request, image_path):
        image_path = request.getfixturevalue(image_path)
        http_image_path = DockerBringupConstants.HTTP_SERVER + image_path
        with allure.step(f"Attempt to install non secure image {http_image_path}"):
            output = self.cli_objects.dut.general.install_image(http_image_path, validate=False)
            assert SecureUpgradeConsts.INVALID_SIGNATURE in output, \
                "An unsigned or improperly signed image was installed successfully on a secure boot enabled switch. " \
                "Seem that the BIOS secure boot validation is broken. " \
                "Please check whether the secure boot option is enabled in the BIOS of this system " \
                "before reporting an issue."

    def restore_kernel_module(self):
        """
        This function will restore kernel module installation status
        """
        logger.info(f"Find kernel module: {SonicSecureBootConsts.KERNEL_MODULE_FILE}")
        kernel_module_path = self.cli_objects.dut.general.find(SonicSecureBootConsts.USR_LIB_MODULES_PATH,
                                                               SonicSecureBootConsts.KERNEL_MODULE_FILE)

        logger.info(f"Check kernel module {SonicSecureBootConsts.KERNEL_MODULE_NAME} installation status")
        lsmod_output = self.cli_objects.dut.general.check_module_status(SonicSecureBootConsts.KERNEL_MODULE_NAME)

        if SonicSecureBootConsts.KERNEL_MODULE_NAME in lsmod_output:
            return
        else:
            logger.info(f"Restore kernel module {SonicSecureBootConsts.KERNEL_MODULE_NAME} original status")
            self.cli_objects.dut.general.install_module(kernel_module_path)

    def _download_image(self, http_image_path):
        """
        Download an image to the switch via HTTP with retry.
        Returns the local file path of the downloaded image.
        """
        image_name = http_image_path.split('/')[-1]
        local_image_file = '/tmp/' + image_name
        logger.info('Starting download sonic image via http')
        try:
            resolv_conf = self.serial_engine.run_cmd_and_get_output('cat /etc/resolv.conf')
            logger.info(f"ONIE DNS info /etc/resolv.conf:{resolv_conf}")
        except Exception as e:
            logger.warning(f"Failed to read /etc/resolv.conf: {e}")
        download_image_cmd = f"wget -O {local_image_file} {http_image_path}"
        retry_call(self.serial_engine.run_cmd,
                   fargs=[download_image_cmd, "100%"],
                   fkwargs={"timeout": 300},
                   tries=5, delay=2, logger=logger)
        return local_image_file

    def _download_install_image(self, image_path):
        """
        This function will download and install the image on the switch
        """
        with allure.step("Download the image"):
            full_image_path = DockerBringupConstants.HTTP_SERVER + image_path
            local_image_file = self._download_image(full_image_path)

        with allure.step(f"Install {local_image_file}"):
            self.serial_engine.run_cmd(f'onie-nos-install {local_image_file}', 'Installed.*base image.*successfully',
                                       SonicSecureBootConsts.SWITCH_RECOVER_TIMEOUT)

    def boot_from_onie(self, restore_image_path=None):
        """
        This function will boot the switch from ONIE to SONiC. If the restore_image_path is provided, it will install
        the image before the boot.
        """
        logger.info("Disconnect engine connection")
        self.cli_objects.dut.general.engine.disconnect()
        self.remove_staged_fw_pkg()

        if restore_image_path:
            with allure.step("Installing restore image {} on the switch".format(restore_image_path)):
                self._download_install_image(restore_image_path)
        else:
            with allure.step("Reboot the switch in ONIE"):
                self.serial_engine.run_cmd('reboot')

        with allure.step("ping till down after reboot from ONIE"):
            ping_till_alive(should_be_alive=False, destination_host=self.serial_engine.ip)

        with allure.step("ping till alive after system is down"):
            ping_till_alive(should_be_alive=True, destination_host=self.serial_engine.ip)

        with allure.step("Verify dockers are up"):
            self.cli_objects.dut.general.verify_dockers_are_up()

        with allure.step('Save the initial configuration'):
            self.cli_objects.dut.general.save_configuration()

        with allure.step("Login from serial port"):
            self._wait_and_login_serial()

    def _wait_and_login_serial(self, login_timeout=180):
        """
        Wait for the serial console to reach the login prompt before sending credentials.
        This avoids the race condition where username is sent while boot logs are still
        streaming through the serial buffer, causing the username to be lost.
        Only match LOGIN_REGEX (not shell prompts) to avoid being fooled by stale
        buffer content like "ONIE:~ #" left over from before a reboot.
        """
        def wait_for_login_prompt():
            _, respond = self.serial_engine.run_cmd('\r', [DefaultConnectionValues.LOGIN_REGEX], timeout=10)
            return respond == 0

        logger.info("Waiting for serial console login prompt")
        success = wait_until(login_timeout, 1, 0, wait_for_login_prompt)
        if not success:
            raise RuntimeError(f"Serial console did not reach login prompt within {login_timeout} seconds")

        self.serial_engine.run_cmd(DefaultConnectionValues.DEFAULT_USER,
                                   [DefaultConnectionValues.PASSWORD_REGEX] +
                                   DefaultConnectionValues.DEFAULT_PROMPTS)
        self.serial_engine.run_cmd(DefaultConnectionValues.DEFAULT_PASSWORD,
                                   DefaultConnectionValues.DEFAULT_PROMPTS)

    def ensure_onie_mode(self):
        output, respond = self.serial_engine.run_cmd('\r', ["ONIE:"],
                                                     timeout=3)
        if respond != 0:
            raise RuntimeError(f"Switch is not in ONIE mode. Output: {output}")

    def login_into_onie_mode(self, topology_obj=None):
        """
        This function will login into onie mode
        After secure boot violation, the switch may be stuck in GRUB menu, so we need to handle that first
        """
        # After a secure-boot violation the DUT is stuck in a 'bad shim' boot
        # loop and cannot boot SONiC. Power-cycle it so the firmware lands
        # cleanly on the grub menu, then enter ONIE via the grub-shell
        # 'configfile' flow (see enter_onie_from_grub_menu_by_serial), which
        # loads the ONIE partition grub.cfg directly - no chainload, no reliance
        # on the UEFI BootOrder (sonic-buildimage#24249, Redmine #5261704).
        try:
            if topology_obj is not None:
                self.cli_objects.dut.general.remote_reboot(
                    topology_obj, boot_into_onie=True, wait_till_alive=False)
            else:
                self.serial_engine.run_cmd('\x1b', ["\\*ONIE", "\\*SONiC", "GNU GRUB"], timeout=15)
                self.cli_objects.dut.general.enter_onie_from_grub_menu_by_serial(self.serial_engine)
        except Exception as e:
            logger.info(f"No GRUB menu detected or already past it: {str(e)}")

        def press_enter_until_onie_login():
            _, respond = self.serial_engine.run_cmd('\r', [DefaultConnectionValues.LOGIN_REGEX] +
                                                    DefaultConnectionValues.DEFAULT_PROMPTS, timeout=5)
            if respond == 0:
                return True
            return False
        logger.info(f"Polling for {SonicSecureBootConsts.ONIE_TIMEOUT} seconds to login into ONIE mode")
        success = wait_until(SonicSecureBootConsts.ONIE_TIMEOUT, 10, 0, press_enter_until_onie_login)
        if success:
            self.serial_engine.run_cmd(DefaultConnectionValues.ONIE_USERNAME, [DefaultConnectionValues.PASSWORD_REGEX] +
                                       DefaultConnectionValues.DEFAULT_PROMPTS)
            self.serial_engine.run_cmd(DefaultConnectionValues.ONIE_PASSWORD, DefaultConnectionValues.DEFAULT_PROMPTS)
        self.serial_engine.run_cmd('\r', DefaultConnectionValues.DEFAULT_PROMPTS)
        self.ensure_onie_mode()

        self.serial_engine.run_cmd_and_get_output('onie-stop')

    def get_into_onie_mode(self, topology_obj):
        """
        This function will get into onie mode
        """
        self.cli_objects.dut.general.prepare_onie_reboot_script_on_dut()
        onie_reboot_script_cmd = '/tmp/onie_reboot.sh install'
        topology_obj.players['dut']['engine'].send_config_set([onie_reboot_script_cmd], exit_config_mode=False,
                                                              cmd_verify=False)
        logger.info("Login to ONIE config mode")
        self.login_into_onie_mode(topology_obj)

    def manipulate_signature(self, test_server_engine, filepath):
        """
        This function will echo random string to the end of filename given
        and by that simulating signature change
        :param test_server_engine: test server engine fixture
        :param filepath: can be any absolute file on the SWITCH!, but will be used for these files:
            [grubx64.efi, mmx64.efi,  shimx64.efi], must be in the format /../../../filename
        """
        # extract file name
        filename = os.path.split(filepath)[1]
        with allure.step("Backup original signature: {} ".format(filename)):
            self.engines.dut.run_cmd('cp -f {} {}/{}'.format(filepath, SecureBootConsts.TMP_FOLDER,
                                                             filename + SonicSecureBootConsts.ORIGIN_TAG))

        with allure.step(f"Uploading {filename} to {SecureBootConsts.TMP_FOLDER} "
                         f"directory on the local device in order to manipulate it locally"):
            self.engines.dut.upload_file_using_scp(test_server_engine.username,
                                                   test_server_engine.password,
                                                   test_server_engine.ip,
                                                   filepath,
                                                   SecureBootConsts.TMP_FOLDER)

        # manipulate file sig
        with allure.step("manipulating signature to file {}".format(filename)):
            test_server_engine.run_cmd('sudo chmod 777 {}/{}'.
                                       format(SecureBootConsts.TMP_FOLDER, filename))
            random_string = ''.join(random.choices(string.ascii_uppercase + string.digits, k=random.randint(5, 10)))
            fileObject = open(SecureBootConsts.TMP_FOLDER + '/{}'.format(filename), "ab")
            # manipulate sig in the [SIG_START,SIG_END] range
            fileObject.seek(random.randint(SecureBootConsts.SIG_START, SecureBootConsts.SIG_END), os.SEEK_END)
            fileObject.write(random_string.encode())
            fileObject.close()

        with allure.step("Uploading back {} to switch".format(filename)):
            test_server_engine.upload_file_using_scp(self.serial_engine.username,
                                                     self.serial_engine.password,
                                                     self.serial_engine.ip,
                                                     SecureBootConsts.TMP_FOLDER + '/{}'.
                                                     format(filename),
                                                     SecureBootConsts.TMP_FOLDER)
            self.serial_engine.run_cmd(SecureBootConsts.ROOT_PRIVILAGE)

        with allure.step("Replace original {} with modified file".format(filepath)):
            self.serial_engine.run_cmd('cp {}/{} {}'.format(SecureBootConsts.TMP_FOLDER, filename,
                                                            '/'.join(filepath.split('/')[0:-1])))

    def get_reboot_cmd(self, test_type, platform):
        """
        This function will return the reboot command for specific test type(shim/grub/vmlinuz)
        """
        if test_type in [SonicSecureBootConsts.SHIM, SonicSecureBootConsts.GRUB] or 'sn5640' in platform or 'sn6600' in platform:
            reboot_cmd = SecureBootConsts.REBOOT
        else:
            reboot_cmd = random.choice(SonicSecureBootConsts.COLD_FAST_WARM_REBOOT_LIST)
        return reboot_cmd

    def unsigned_file_secure_boot(self, secure_component, test_server_engine, test_type, platform):
        """
        This function will perform as the test body called by the different wrappers
        It will perform the following:
            1. change the signature for secure boot component given by filename
            2. reboot/fast-reboot/warm-reboot
                a. reboot and fast-reboot work for shim/grub/vmlinuz
                b. warm-reboot only works for vmlinuz
            3. validate 'invalid signature message' appears
        """
        self.manipulate_signature(test_server_engine, secure_component)

        reboot_cmd = self.get_reboot_cmd(test_type, platform)
        logger.info(f"Executing command: {reboot_cmd} for {test_type} validation")
        self.serial_engine.run_cmd(reboot_cmd, SonicSecureBootConsts.INVALID_SIGNATURE, timeout=SonicSecureBootConsts.SWITCH_RECOVER_TIMEOUT)

    def signed_kernel_module_secure_boot(self):
        """
        This function will perform as the test body called by test function
        It will perform the following:
            1. uninstall a kernel module
            2. install the signed kernel module
            3. validate the installation is successfully
        """
        kernel_module_path = self.uninstall_kernel_module(SonicSecureBootConsts.KERNEL_MODULE_FILE,
                                                          SonicSecureBootConsts.KERNEL_MODULE_NAME)

        with allure.step(f"Install signed kernel module: {SonicSecureBootConsts.KERNEL_MODULE_NAME}"):
            self.cli_objects.dut.general.install_module(kernel_module_path)

        with allure.step(f"Validate kernel module: {SonicSecureBootConsts.KERNEL_MODULE_NAME} "
                         f"is installed successfully"):
            lsmod_output = self.cli_objects.dut.general.check_module_status(SonicSecureBootConsts.KERNEL_MODULE_NAME)
        assert SonicSecureBootConsts.KERNEL_MODULE_NAME in lsmod_output, \
            f"{SonicSecureBootConsts.KERNEL_MODULE_NAME} is not removed!"

    def un_signed_kernel_module_secure_boot(self):
        """
        This function will perform as the test body called by test function
        It will perform the following:
            1. uninstall a kernel module
            2. extract the key from the kernel module
            3. install the unsigned kernel module
            4. validate the installation would be blocked by secure boot
        """
        with allure.step(f"Uninstall kernel module: {SonicSecureBootConsts.KERNEL_MODULE_NAME}"):
            kernel_module_path = self.uninstall_kernel_module(SonicSecureBootConsts.KERNEL_MODULE_FILE,
                                                              SonicSecureBootConsts.KERNEL_MODULE_NAME)

        with allure.step(f"Extract key from kernel module: {SonicSecureBootConsts.KERNEL_MODULE_FILE}"):
            self.extract_key_from_kernel_module(kernel_module_path)

        with allure.step(f"Validate kernel module: {SonicSecureBootConsts.KERNEL_MODULE_NAME} installation is blocked"):
            insmod_output = self.cli_objects.dut.general.install_module(
                SonicSecureBootConsts.KERNEL_MODULE_TEMP_FILE_PATH)

            assert SonicSecureBootConsts.KERNEL_MODULE_BLOCK_MESSAGE in insmod_output, \
                f"Unsigned kernel module {SonicSecureBootConsts.KERNEL_MODULE_TEMP_FILE_PATH} installed without block!"

    def onie_secure_boot(self, request, image_path, topology_obj, dut_secure_type):
        """
        This function will perform as the test body called by test function
        It will perform the following:
            1. get image path - unsigned or signature mismatched
            2. get into onie mode
            3. validate the installation would be blocked by secure boot
        """
        with allure.step("Get image path"):
            if image_path == "sig_mismatch_image_path":
                if dut_secure_type == 'dev':
                    # If the DUT secure type is 'dev', use the prod image as the test target image
                    image_path = SecureBootConsts.SIG_MISMATCH_PROD_IMAGE_PATH
                else:
                    # If the DUT secure type is 'prod', use the dev image as the test target image
                    image_path = SecureBootConsts.SIG_MISMATCH_DEV_IMAGE_PATH
            else:
                image_path = SecureBootConsts.NON_SECURE_IMAGE_PATH
            http_image_path = DockerBringupConstants.HTTP_SERVER + image_path
            logger.info(f"The target image path is {http_image_path}")

        with allure.step("Get into ONIE mode"):
            self.get_into_onie_mode(topology_obj)

        with allure.step(f"Download {http_image_path}"):
            local_image_file = self._download_image(http_image_path)

        with allure.step(f"Install {local_image_file}"):
            self.serial_engine.run_cmd(f'onie-nos-install {local_image_file}',
                                       SonicSecureBootConsts.INVALID_SIGNATURE,
                                       SonicSecureBootConsts.SWITCH_RECOVER_TIMEOUT)

    @staticmethod
    def get_unsigned_mismatched_component_info(component, signed_type, dut_secure_type, platform_params):
        """
        This function will return the component url and version
        """
        path_to_current_folder = os.path.abspath(os.path.dirname(os.path.abspath(__file__)))
        fw_pkg_path = f'{path_to_current_folder}/../../{MarsConstants.UPDATED_FW_TAR_PATH}'
        fw_data = extract_fw_data(fw_pkg_path)
        current_platform = platform_params.filtered_platform.upper()
        hw_type = platform_params.filtered_platform.upper()
        url = ""
        version = ""
        try:
            if signed_type == 'unsigned':
                url = fw_data[signed_type][hw_type]["component"][component][0]["firmware"]
            elif signed_type == 'key_mismatched_signed':
                for component_item in fw_data[signed_type][hw_type]["component"][component]:
                    if component_item['type'] != dut_secure_type:
                        url = component_item["firmware"]
                        version = component_item["version"]
            assert url
            return url, version
        except Exception as err:
            err_mgs = f'Can not find component for platform: {current_platform} - sign type: {signed_type} - ' \
                f'component: {component}. Got err: {err}'
            logger.info(err_mgs)
            raise err

    @retry(Exception, tries=5, delay=3)
    def validate_http_reachable(self, url):
        output = self.engines.dut.run_cmd(f'sudo curl -I {url}')
        expected_msg = 'OK'
        if expected_msg not in output:
            raise Exception(f"Expected message {expected_msg} not found in output {output} {url} is not reachable")

    @retry(Exception, tries=5, delay=3)
    def validate_file_exist(self, file_path):
        file_status = self.cli_objects.dut.general.stat(file_path)
        assert file_status["exists"] is True

    def fwutil_install_secure_boot_negative(self, component, signed_type, dut_secure_type, platform_params,
                                            expected_message, timeout, topology_obj=None):
        """
        This function will perform as the test body of fwutil secure boot test
        It will perform the following:
            1. Validate url is reachable
            2. do fwutil install
            3. validate invalid signature message appears
        """

        with allure.step('Get component url'):
            component_url, component_version = self.get_unsigned_mismatched_component_info(
                component, signed_type, dut_secure_type, platform_params)
        with allure.step(f"Validate {SonicSecureBootConsts.GRUB_ENV} file is created"):
            self.validate_file_exist(SonicSecureBootConsts.GRUB_ENV)
        with allure.step(f"Validate {component_url} is reachable"):
            self.validate_http_reachable(component_url)
        with allure.step(f"Check firmware status"):
            self.serial_engine.run_cmd(f'sudo fwutil show status')
        with allure.step(f"Install {component_url}"):
            try:
                self.serial_engine.run_cmd(f'sudo fwutil install chassis component {component} fw {component_url} -y', expected_message, timeout)
            except Exception as e:
                logger.warning(f"Failed to get response from serial console with error: {str(e)}")
        if component == SonicSecureBootConsts.CPLD_COMPONENT:
            # Power cycle is required for CPLD burning
            with allure.step("Power cycle after CPLD installation"):
                self.cli_objects.dut.general.remote_reboot(topology_obj)
            with allure.step("Check the CPLD version is the fail safe CPLD version"):
                # The fail safe CPLD image(KGI image) is burnt in the factory
                # in case the in-use image(CI image) is corrupted or unauthenticated
                current_cpld_version = self.get_fw_components_versions()[SonicSecureBootConsts.CPLD_COMPONENT]
                fail_safe_cpld_version = self.get_fail_safe_cpld_version(platform_params)
                assert current_cpld_version == fail_safe_cpld_version, \
                    "The CPLD should fall back to the fail safe version."

    @staticmethod
    def is_secure_boot_supported(boot_config):
        return 'EFI variables are not supported' not in boot_config

    @staticmethod
    def is_secure_boot_enabled(boot_config):
        return 'SecureBoot enabled' in boot_config

    @staticmethod
    def check_secure_boot_status(boot_config_status):
        """
        This function will check
            1. whether secure boot is supported at current switch, if not, skip the test
            2. whether secure boot is enabled at current switch, if not, skip the test
        """
        with allure.step('Check if Secure Boot is supported'):
            if not SonicSecureBootHelper.is_secure_boot_supported(boot_config_status):
                pytest.skip(SonicSecureBootConsts.SECURE_BOOT_NOT_SUPPORTED_MESSAGE)

        with allure.step('Check if Secure Boot is enabled'):
            if not SonicSecureBootHelper.is_secure_boot_enabled(boot_config_status):
                pytest.fail(SonicSecureBootConsts.SECURE_BOOT_NOT_ENABLED_MESSAGE)

    def copy_kernel_module_to_ngts_docker(self, kernel_module_file_path):
        """
        This function will copy kernel module file from dut to ngts docker
        """
        logger.info(f"Start copying {kernel_module_file_path} from dut to ngts docker")
        self.engines.dut.upload_file_using_scp(self.engines.sonic_mgmt.username, self.engines.sonic_mgmt.password,
                                               getattr(self.engines.sonic_mgmt, 'switch_reachable_ip', self.engines.sonic_mgmt.ip), kernel_module_file_path,
                                               SecureBootConsts.TMP_FOLDER)
        logger.info(f"Delete temp file {kernel_module_file_path}")
        self.engines.dut.run_cmd(f"sudo rm -f {kernel_module_file_path}")

    def copy_kernel_module_to_dut(self, kernel_module_file_path):
        """
        This function will copy kernel module file from ngts docker to dut
        """
        logger.info(f"Start copying {kernel_module_file_path} from ngts docker to dut")
        self.engines.sonic_mgmt.upload_file_using_scp(self.engines.dut.username, self.engines.dut.password,
                                                      self.engines.dut.ip, kernel_module_file_path,
                                                      SecureBootConsts.TMP_FOLDER)
        logger.info(f"Delete temp file {kernel_module_file_path}")
        self.engines.sonic_mgmt.run_cmd(f"sudo rm -f {kernel_module_file_path}")

    def uninstall_kernel_module(self, kernel_module_file, kernel_module_name):
        """
        This function will uninstall
        """
        with allure.step(f"Find kernel module: {kernel_module_file}"):
            kernel_module_path = self.cli_objects.dut.general.find(SonicSecureBootConsts.USR_LIB_MODULES_PATH,
                                                                   kernel_module_file)

        with allure.step(f"Remove kernel module: {kernel_module_name}"):
            self.cli_objects.dut.general.remove_module(kernel_module_name)

        with allure.step(f"Validate kernel module: {kernel_module_name} is uninstalled"):
            lsmod_output = self.cli_objects.dut.general.check_module_status(kernel_module_name)

        assert kernel_module_name not in lsmod_output, f"{kernel_module_name} is still installed!"
        return kernel_module_path

    def extract_key_from_kernel_module(self, kernel_module_path):
        """
        This function will extract key from module
        """
        with allure.step(f"Copy kernel module file to {SecureBootConsts.TMP_FOLDER}"):
            self.cli_objects.dut.general.cp(kernel_module_path, SonicSecureBootConsts.TMP_FOLDER, flags='-f')

        with allure.step("Upload kernel module file to ngts docker"):
            self.copy_kernel_module_to_ngts_docker(SonicSecureBootConsts.KERNEL_MODULE_TEMP_FILE_PATH)

        with allure.step(f"Extract key from {SonicSecureBootConsts.KERNEL_MODULE_FILE}"):
            self.cli_objects.sonic_mgmt.general.extract_key_from_module(
                SonicSecureBootConsts.KERNEL_MODULE_TEMP_FILE_PATH)

        with allure.step("Download extracted kernel module file to dut"):
            self.copy_kernel_module_to_dut(SonicSecureBootConsts.KERNEL_MODULE_TEMP_FILE_PATH)

    def remove_staged_fw_pkg(self):
        """
        This function will remove the staged onie pkg after onie update failure
        """
        self.ensure_onie_mode()
        _, respond = self.serial_engine.run_cmd('onie-fwpkg purge', ["Removing all pending firmware updates (y/N)?"])
        if respond == 0:
            self.serial_engine.run_cmd('y')

    @staticmethod
    def restore_basic_config(topology_obj, setup_name, platform_params, is_air):
        """
        This function will restore basic configuration
        """
        with allure.step("Recover basic config"):
            dut_cli = topology_obj.players['dut']['cli']
            with allure.step("Set dut NTP timezone to {} time.".format('Israel')):
                dut_engine = topology_obj.players['dut']['engine']
                dut_engine.disconnect()
                dut_engine.run_cmd('sudo timedatectl set-timezone {}'.format('Asia/Jerusalem'), validate=True)

            with allure.step("Init telemetry keys"):
                dut_cli.general.init_telemetry_keys()

            with allure.step("Apply basic config"):
                dut_cli.general.apply_basic_config(topology_obj, setup_name, platform_params, disable_ztp=True,
                                                   is_air=is_air)

    def get_fw_components_versions(self):
        """
        Get dictionary with component name as key and version as value
        :param engine: dut engine
        :return: dictionary with component name as key and version as value
        """
        self.engines.dut.disconnect()
        fwutil_show_status_output = self.engines.dut.run_cmd('sudo fwutil show status')
        fwutil_show_status_dict = generic_sonic_output_parser(fwutil_show_status_output)
        component_names_list = fwutil_show_status_dict[0]['Component']
        component_versions_list = fwutil_show_status_dict[0]['Version']
        component_versions_dict = {}
        for component, version in zip(component_names_list, component_versions_list):
            component_versions_dict[component] = version
        return component_versions_dict

    @staticmethod
    def restore_cpld(cli_objects, engines, topology_obj, platform_params, cpld):
        """
        Restore the CPLD to the expected latest one defined in firmware.json
        """
        cpld_component_data = SonicSecureBootHelper.get_component_data(platform_params, cpld)
        url, latest_cpld_version = SonicSecureBootHelper.get_latest_expected_cpld(cpld_component_data, cpld)
        with allure.step(f"Restore the cpld back to {url}"):
            engines.dut.run_cmd(f'sudo fwutil install chassis component {cpld} fw {url} -y',)

        with allure.step("Power cycle after CPLD installation"):
            cli_objects.dut.general.remote_reboot(topology_obj)

    @staticmethod
    def get_component_data(platform_params, component):
        """
        Get the component data from the firmware.json file.
        """
        path_to_current_folder = os.path.abspath(os.path.dirname(os.path.abspath(__file__)))
        fw_pkg_path = f'{path_to_current_folder}/../../{MarsConstants.UPDATED_FW_TAR_PATH}'
        fw_data = extract_fw_data(fw_pkg_path)
        hw_type = normalize_firmware_chassis_name(platform_params.filtered_platform)
        dut_name = platform_params['setup_name'].strip('_setup').split('_')[-1]
        component_data = None
        try:
            if 'host' in fw_data:
                for defined_dut_name in fw_data['host'].keys():
                    if defined_dut_name == dut_name:
                        component_data = fw_data['host'][dut_name]["component"][component]
                        break
            if not component_data:
                component_data = fw_data["chassis"][hw_type]["component"][component]
        except KeyError:
            assert False, f"The component {component} of dut {dut_name} is not found in firmware.json"
        return component_data

    @staticmethod
    def get_latest_expected_cpld(cpld_component_data, cpld):
        """
        Get the expected latest CPLD url and version defined in firmware.json
        """
        with allure.step(f'Getting list of versions for {cpld} from firmware.json'):
            url = cpld_component_data[0]['firmware']
            latest_cpld_version = cpld_component_data[0]['version']
            logger.info(f"Latest CPLD version: {latest_cpld_version}, url: {url}")

        return url, latest_cpld_version

    @staticmethod
    def get_fail_safe_cpld_version(platform_params):
        """
        Get the fail safe cpld version of a specific setup
        Currently the version is pre-defined in the constants.
        It'd be better to dynamically get it from the dut after we get the method.
        """
        try:
            return SonicSecureBootConsts.FAIL_SAFE_CPLD_VERSION[platform_params.setup_name]
        except KeyError:
            raise Exception("The fail safe CPLD version is not defined for setup {}, need to add it.".format(
                platform_params.setup_name))
