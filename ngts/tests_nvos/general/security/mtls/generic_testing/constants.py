from ngts.tests_nvos.general.security.certificate.constants import TestCert

CERTIFICATE = 'certificate'
CA_CERTIFICATE = 'ca-certificate'
MTLS = 'mtls'

TEST_CERTS = [TestCert.cert_valid_1, TestCert.cert_valid_2, TestCert.cert_valid_3]
TEST_CERTS = [cert.copy(f'api-test-{cert.name}') for cert in TEST_CERTS]
TEST_CACERT_NAMES = [cert.cacert_name for cert in TEST_CERTS]


class Errors:
    CERT_DONT_EXIST = "certificate `{cert_id}` doesn't exist"
    # nvued wraps this in "Error: Action failed with the following issue:", so the message is not prefixed by "Error:".
    INSTALLED_CA_DELETE_ERR = ("The CA certificate `{ca_id}` is currently being used "
                               "by the following applications: ['{app_name}']")
    FAILED_TO_VERIFY_SERVER_ERR = 'curl failed to verify the legitimacy of the server'
    NO_CERT_WAS_SENT_ERR = 'No required SSL certificate was sent'
    SSL_CERT_ERR = 'SSL certificate error'
    MTLS_ERRORS = [FAILED_TO_VERIFY_SERVER_ERR, NO_CERT_WAS_SENT_ERR, SSL_CERT_ERR]
