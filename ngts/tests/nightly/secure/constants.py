'''
This file includes the constants used
for secure boot testing
'''


class SecureBootConsts:
    '''
    class contains consts for secure boot
    '''
    ROOT_PRIVILAGE = 'sudo su'
    TMP_FOLDER = '/tmp'
    MOUNT_FOLDER = '/UEFI'
    IMAGE_PATH = ["non_secure_image_path", "sig_mismatch_image_path"]
    NON_SECURE_IMAGE_PATH = "/auto/sw_regression/system/SONIC/security/secure_boot/non_signed_image/sonic-mellanox.bin"
    SIG_MISMATCH_BASE_PATH = "/auto/sw_regression/system/SONIC/security/secure_boot/sig_mismatch_image"
    SIG_MISMATCH_DEV_IMAGE_PATH = f"{SIG_MISMATCH_BASE_PATH}/sonic-mellanox-dev.bin"
    SIG_MISMATCH_PROD_IMAGE_PATH = f"{SIG_MISMATCH_BASE_PATH}/sonic-mellanox-mismatch-prod-sig.bin"
    EFI_PARTITION_CMD = "fdisk -l | grep \"EFI System\" | awk \'{print $1}\'"
    LAST_OCCURENCE_REGEX = "({})(?!.*\1)"
    VMILUNZ_REGEX = '(vmlinuz-.*-amd64)'
    VMILUNZ_DIR = '/boot/'
    INVALID_SIGNATURE = ["Invalid signature detected",
                         "Malformed binary after Attribute Certificate Table",
                         "bad.*signature",
                         "CMS Verification Failure",
                         "Failure: CMS signature verification failed",
                         "kexec_file_load failed: Key was rejected by service"]
    SECURE_BOOT_NOT_ENABLED_MESSAGE = 'Secure Boot function is not enabled in UEFI'
    SECURE_BOOT_NOT_SUPPORTED_MESSAGE = 'Secure Boot function is not supported at this switch'
    REBOOT = "sudo reboot -f"
    WORKSPACE_PATH = '/root/mars/workspace/'

    # numerical expressions
    SLEEP_AFTER_ONIE_INSTALL = 45
    SIG_START = -10
    SIG_END = -1

    FILE_DEFAULT_SCP_TIMEOUT = 240


class SonicSecureBootConsts(SecureBootConsts):
    '''
    class contains consts for secure boot
    '''
    SHIM = 'shim'
    GRUB = 'grub'
    GRUB_ENV = '/host/grub/grubenv'
    VMLINUZ = 'vmlinuz'
    ORIGIN_TAG = '_origin'
    ONIE_COMPONENT = 'ONIE'
    BIOS_COMPONENT = 'BIOS'
    CPLD_COMPONENT = 'CPLD2'
    FWUTIL_UNSIGNED = 'unsigned'
    FWUTIL_KEY_MISMATCHED_SIGNED = 'key_mismatched_signed'
    FWUTIL_ONIE_TEST_NAME = 'test_fwutil_install_onie_key_check_fail'
    LOCAL_SECURE_BOOT_DIR = '/auto/sw_regression/system/SONIC/security/secure_boot'
    EFI_SECURE_COMPONENT = '{}/EFI/SONiC-OS/{}'
    SHIM_FILEPATH = EFI_SECURE_COMPONENT.format(SecureBootConsts.MOUNT_FOLDER, 'shimx64.efi')
    GRUB_FILEPATH = EFI_SECURE_COMPONENT.format(SecureBootConsts.MOUNT_FOLDER, 'grubx64.efi')
    KERNEL_MODULE_NAME = 'leds_mlxreg'
    KERNEL_MODULE_FILE = 'leds-mlxreg.ko'
    KERNEL_MODULE_TEMP_FILE_PATH = SecureBootConsts.TMP_FOLDER + '/' + KERNEL_MODULE_FILE
    USR_LIB_MODULES_PATH = '/usr/lib/modules'
    KERNEL_MODULE_BLOCK_MESSAGE = 'Key was rejected by service'
    INVALID_SIGNATURE_EXPECTED_MESSAGE = {
        ONIE_COMPONENT:
            ["Invalid signature detected",
             "Malformed binary after Attribute Certificate Table",
             "bad.*signature",
             "Key was rejected by service",
             "CMS signature verification failed"],
        BIOS_COMPONENT:
            ["ONIE: ERROR: bios_update firmware update.* attempt: 3"],
        CPLD_COMPONENT:
            [".*PASS!.*"]}
    REBOOT = "sudo reboot -f"
    FAST_REBOOT = "sudo fast-reboot -f -d"
    WARM_REBOOT = "sudo warm-reboot -f -d"
    COLD_FAST_WARM_REBOOT_LIST = [REBOOT, FAST_REBOOT, WARM_REBOOT]
    FAIL_SAFE_CPLD_VERSION = {"sonic_moose_r-moose-02": "CPLD000330_REV0500",
                              "CI_sonic_SPC4_1": "CPLD000330_REV0500"}

    SWITCH_RECOVER_TIMEOUT = 300
    CPLD_BURNING_RECOVER_TIMEOUT = 1200
    ONIE_TIMEOUT = 180
    PROD_CORRUPT_MFA_PATH = '/auto/sw_regression/system/SONIC/MARS/security/secure_boot/corrupt_mfa/OPN/'
    DEV_CORRUPT_MFA_PATH = '/auto/sw_regression/system/SONIC/MARS/security/secure_boot/corrupt_mfa/IPN/'
    PROD_CORRUPT_MFA_FILE = 'prod_corrupted_bin'
    DEV_CORRUPT_MFA_FILE = 'dev_corrupted_bin'
    MFA_FILE_PATH = "/etc/mlnx"
    DEV_CORRUPT_MFA_ERR_MSG = "Rejected authentication"
    PROD_CORRUPT_MFA_ERR_MSG = "Bad parameter"
    SECURE_FW_MSG = "Security Attributes:   secure-fw"
    SECURE_FW_DEV_MSG = "Security Attributes:   secure-fw, dev"
    BISON_ONIE_TIMEOUT_FACTOR = 1.5
    PLATFORM_THAT_DO_NOT_SUPPORT_MISMATCH_SIGNATURE_TEST = ['x86_64-nvidia_sn6600_ld-r0', 'x86_64-nvidia_sn6600-r0', 'x86_64-nvidia_sn6810_ld-r0']
    MISMATCH_SIGNATURE_SIGNED_TYPE = ["key_mismatched_signed", "sig_mismatch_image_path"]


class SecureUpgradeConsts:
    '''
    class contains consts for secure upgrade
    '''
    IMAGE_PATH = ("non_secure_image_path", "sig_mismatch_image_path")
    INVALID_SIGNATURE = "CMS signature Verification Failed"


class SonicSecureUpgradeConsts(SecureUpgradeConsts):
    '''
    class contains consts for secure upgrade
    '''
    pass
