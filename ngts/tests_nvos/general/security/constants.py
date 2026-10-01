MINUTE = 60
YEAR = 365
MAX_TEST_TIMEOUT = 15 * MINUTE
SSN_OPTIONS = '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'

TMP_TEST_CERTS_DIR = '/auto/sw_system_project/NVOS_INFRA/security/verification/certs/test_certs/tmp_test_certs'


class SecurityConsts:
    SED_DEFAULT_PASSWORD = "xc|[Ee4kX*a\\c/6V&57d^eP!zUOK]-*0"
    # Runtime policy: SED passwords are 8-127 characters with no whitespace.
    SED_MIN_PASSWORD_LENGTH = 8
    SED_MAX_PASSWORD_LENGTH = 127
    CRL_EXPIRED_FILE_PATH = "/auto/sw_system_project/NVOS_INFRA/verification_files/crl/expired_crl.crl"


ETC_HOSTS = '/etc/hosts'
