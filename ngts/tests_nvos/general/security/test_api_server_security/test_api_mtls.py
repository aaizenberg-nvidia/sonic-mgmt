import random
import pytest

from ngts.nvos_constants.constants_nvos import ApiType, TestFlowType, RebootTestFlowType
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.general.security.mtls.generic_testing.generic_mtls_testing import generic_test_mtls_cli, \
    generic_test_mtls_set_bad_param, generic_test_mtls_set_ca_without_cert_not_rejected, \
    generic_test_mtls_core_functionality, generic_test_mtls_delete_installed_ca, generic_test_mtls_reboot, \
    generic_mtls_factory_reset_no_params_check, generic_mtls_factory_reset_keep_all_config_check, \
    generic_mtls_upgrade_check
from ngts.tests_nvos.general.security.security_test_tools.constants import AddressingType
from ngts.tests_nvos.general.security.test_api_server_security.constants import ApiConsts, API_INSTALLED
from ngts.tests_nvos.general.security.test_api_server_security.helpers import cleanup_mtls_test, setup_mtls_checker, \
    run_curl_and_verify


@pytest.mark.mtls
@pytest.mark.security
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_api_mtls_cli(test_api):
    """
    Verify that all CLI work and check values change properly in show

    1. Run show commands
    2. Verify outputs contain the required fields
    3. Set ca-certificate
    4. Verify in show commands
    5. Unset
    6. Verify in show commands
    """
    generic_test_mtls_cli(test_api, System().api, ApiConsts.Mtls.fields, API_INSTALLED)


@pytest.mark.mtls
@pytest.mark.security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_api_mtls_set_bad_param(test_api):
    """
    Verify that set with bad param rejected

    1. Set api ca-certificate with bad param (CERT-ID or non existing/imported id)
    2. Verify command rejected
    3. Verify in show – expect no ca-cert installed to api
    """
    generic_test_mtls_set_bad_param(test_api, System().api)


@pytest.mark.mtls
@pytest.mark.security
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_api_mtls_set_ca_without_cert_not_rejected(test_api):
    """
    Verify that set api CA not rejected when no cert was previously set

    1. Set CA
    2. Verify command success
    3. Verify in show – expect ca to be installed to api
    """
    generic_test_mtls_set_ca_without_cert_not_rejected(test_api, System().api, API_INSTALLED)


@pytest.mark.mtls
@pytest.mark.security
@pytest.mark.parametrize('addressing_type', [AddressingType.IPV4, AddressingType.IPV6])
def test_api_mtls_core_functionality(addressing_type, dut_ipv6_addr, certs_no_import):
    generic_test_mtls_core_functionality(addressing_type, dut_ipv6_addr, System().api, run_curl_and_verify,
                                         certs_no_import)


@pytest.mark.mtls
@pytest.mark.security
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
def test_api_mtls_delete_installed_ca(test_flow, engines, local_adminuser, scp_player):
    """
    Verify that delete of ca-cert that is installed to api rejected

    1. Set api ca-certificate
    2. Try to delete that ca-certificate
    3. Verify reject
    4. Verify in show – expect ca-cert still installed
    5. Verify client cant request without suitable cert – expect fail
    """
    generic_test_mtls_delete_installed_ca(test_flow, engines, scp_player, local_adminuser, System().api,
                                          API_INSTALLED, run_curl_and_verify, True)


@pytest.mark.reboot
@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.mtls
@pytest.mark.security
@pytest.mark.parametrize('reboot_flow', RebootTestFlowType.ALL_TYPES)
def test_api_mtls_reboot(reboot_flow, engines):
    """
    Verify mtls config and functionality after reboot

    1. Set api certificate & ca-certificate
    2. Save / no save
    3. Reboot
    4. Verify config in show
    5. Verify REST connection
    """
    generic_test_mtls_reboot(reboot_flow, engines, System().api, API_INSTALLED, run_curl_and_verify, True)


# generator functions

api_mtls_factory_reset_no_params_check = generic_mtls_factory_reset_no_params_check(setup_mtls_checker,
                                                                                    cleanup_mtls_test,
                                                                                    System().api, API_INSTALLED,
                                                                                    run_curl_and_verify,
                                                                                    True)

api_mtls_factory_reset_keep_all_config_check = generic_mtls_factory_reset_keep_all_config_check(setup_mtls_checker,
                                                                                                cleanup_mtls_test,
                                                                                                System().api,
                                                                                                API_INSTALLED,
                                                                                                run_curl_and_verify,
                                                                                                True)

api_mtls_upgrade_check = generic_mtls_upgrade_check(setup_mtls_checker, cleanup_mtls_test, System().api, API_INSTALLED,
                                                    run_curl_and_verify, True)
