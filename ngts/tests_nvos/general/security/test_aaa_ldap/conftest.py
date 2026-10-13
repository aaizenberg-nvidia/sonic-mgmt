import pytest

from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.helpers import remove_etc_host_mapping_to_dn, add_etc_host_mapping_to_dn
from ngts.tests_nvos.general.security.security_test_tools.constants import AddressingType
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.RemoteAaaServerInfo import dut_lab
from ngts.tests_nvos.general.security.test_aaa_ldap.constants import LdapConsts
from ngts.tests_nvos.helpers.pytest_helpers import get_cur_test_param_value
from ngts.tools.test_utils import allure_utils as allure


@pytest.fixture(scope='session', autouse=True)
def prepare_base_aaa_config(engines, devices):
    """
    @summary: Add/remove base aaa config needed for ldap tests on cumulus switches
    """
    devices.dut.setup_base_aaa_config(engines.dut)
    yield
    devices.dut.cleanup_base_aaa_config(engines.dut)


@pytest.fixture(scope='function', autouse=True)
def recover_after_aaa(cleanup_after_aaa):
    return


@pytest.fixture(scope='function', autouse=False)
def alias_ldap_server_dn(engines, request, topology_obj):
    """
    @summary: Alias ldap.itzgeek.local to the VM LDAP address for this DUT's lab.
        Remove the alias after the test.
    """
    server_cert_dn = 'ldap.itzgeek.local'
    connection_method = get_cur_test_param_value(request, 'addressing_type')
    addressing_type = AddressingType.IPV4 if connection_method == AddressingType.IPV4 else AddressingType.IPV6
    site, _, addresses = dut_lab(topology_obj)
    server_address = addresses[addressing_type]
    remove_etc_host_mapping_to_dn(server_cert_dn, engines.dut)
    with allure.step(f'Alias {server_cert_dn} to {server_address} (site: {site or "unknown"})'):
        add_etc_host_mapping_to_dn(server_cert_dn, server_address, engines.dut)

    yield

    with allure.step('After tests: Unset ldap config, while the server alias still resolves'):
        # The server is configured by hostname (the certificate CN must match a name, not an IP).
        # Removing the alias while that config is still in place leaves the switch pointing at a
        # name nothing can resolve, so every nsswitch ldap lookup floods syslog with nslcd bind
        # timeouts that log analyzer then attributes to unrelated tests (NVBug 6530355).
        System().aaa.ldap.unset(apply=True, ask_for_confirmation=True, dut_engine=engines.dut).ignore_result()

    with allure.step('After tests: Remove docker ldap server alias from the switch'):
        remove_etc_host_mapping_to_dn(server_cert_dn, engines.dut)


@pytest.fixture(scope='function', autouse=False)
def backup_and_restore_certificates(engines, devices):
    """
    @summary: To allow the switch work with the docker ldap server with cert-verify enabled,
        we need to get the right certificate, which is kept in specific shared location.
    """
    # older/manual implementation
    # with allure.step('Before tests: Add ldap server certificate'):
    #     with allure.step('Backup original certificates file'):
    #         engines.dut.run_cmd(f'sudo cp -f {LdapConsts.SWITCH_CA_FILE} {LdapConsts.SWITCH_CA_BACKUP_FILE}')

    yield

    System().security.ca_certificate.cert_id[LdapConsts.CA_CERT_ID].action_delete().ignore_result()

    # older/manual implementation
    # with allure.step('After tests: Restore certificates file'):
    #     engines.dut.run_cmd(f"sudo mv -f {LdapConsts.SWITCH_CA_BACKUP_FILE} {LdapConsts.SWITCH_CA_FILE}")
    #
    # with allure.step('Restart nslcd service'):
    #     # On cumulus switch, nslcd is already stopped and should not be running, so no need to restart it.
    #     if TestToolkit.is_eth_dut():
    #         return
    #
    #     engines.dut.run_cmd('sudo service nslcd restart')
    #     sleep(3)
