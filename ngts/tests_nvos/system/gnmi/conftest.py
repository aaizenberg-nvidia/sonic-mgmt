import logging

import pytest

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.tests_nvos.general.security.helpers import import_certs_safely, import_cas_safely, delete_all_imported_certs, \
    delete_all_imported_cas
from ngts.tests_nvos.general.security.security_test_tools.constants import AddressingType
from ngts.tests_nvos.helpers.pytest_helpers import get_cur_test_param_value
from ngts.tests_nvos.system.gnmi.helpers import get_scp_player, verify_gnmi_client_tools_installed, \
    setup_gnmi_cert_tests, cleanup_gnmi_cert_tests
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.fixture(scope='session')
def scp_player(engines) -> LinuxSshEngine:
    return get_scp_player(engines)


@pytest.fixture(scope='session', autouse=True)
def verify_gnmi_client_tools_installed_on_player():
    verify_gnmi_client_tools_installed()


@pytest.fixture(scope='module')
def gnmi_certs_ipv4(engines, dut_hostname, scp_player):
    tmp_certs_dir, certs = setup_gnmi_cert_tests(engines, dut_hostname, scp_player, import_to_dut=False)
    yield certs
    cleanup_gnmi_cert_tests(tmp_certs_dir, certs, certs)


@pytest.fixture(scope='module')
def gnmi_certs_ipv6(engines, dut_hostname, scp_player, dut_ipv6_addr):
    tmp_certs_dir, certs = setup_gnmi_cert_tests(engines, dut_hostname, scp_player, dut_ipv6_addr, import_to_dut=False)
    yield certs
    cleanup_gnmi_cert_tests(tmp_certs_dir, certs, certs)


@pytest.fixture
def gnmi_certs(request, gnmi_certs_ipv4, gnmi_certs_ipv6, scp_player):
    certs = gnmi_certs_ipv6 if get_cur_test_param_value(request, 'addressing_type') == AddressingType.IPV6 else gnmi_certs_ipv4
    with allure.step('import certs to dut'):
        import_certs_safely(certs, scp_player, False)
    with allure.step('import cas to dut'):
        import_cas_safely(certs, scp_player, False, False)
    return certs


@pytest.fixture
def gnmi_certs_no_import(request, gnmi_certs_ipv4, gnmi_certs_ipv6):
    with allure.step('delete existing certs & cas'):
        delete_all_imported_certs()
        delete_all_imported_cas()
    if get_cur_test_param_value(request, 'addressing_type') == AddressingType.IPV6:
        return gnmi_certs_ipv6
    else:
        return gnmi_certs_ipv4
