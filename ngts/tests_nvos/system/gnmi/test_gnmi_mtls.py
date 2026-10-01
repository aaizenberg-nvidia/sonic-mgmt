import random
import subprocess
import time
from typing import List

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ApiType, TestFlowType, RebootTestFlowType
from ngts.nvos_tools.infra.CmdRunner import CmdRunner
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.conftest import local_adminuser
from ngts.tests_nvos.general.security.mtls.generic_testing.constants import CA_CERTIFICATE
from ngts.tests_nvos.general.security.mtls.generic_testing.generic_mtls_testing import generic_test_mtls_cli, \
    generic_test_mtls_set_bad_param, generic_test_mtls_set_ca_without_cert_not_rejected, \
    generic_test_mtls_core_functionality, generic_test_mtls_delete_installed_ca, generic_test_mtls_reboot, \
    generic_mtls_factory_reset_no_params_check, generic_mtls_factory_reset_keep_all_config_check, \
    generic_mtls_upgrade_check
from ngts.tests_nvos.general.security.security_test_tools.constants import AddressingType
from ngts.tests_nvos.helpers.general_helpers import run_cmd
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmicCmdBuilder
from ngts.tests_nvos.system.gnmi.constants import GNMI_INSTALLED, CERTIFICATE, GnmicErr, GnmiMode
from ngts.tests_nvos.system.gnmi.helpers import run_gnmi_client_and_verify, setup_gnmi_mtls_checker, \
    cleanup_gnmi_cert_tests, change_interface_description, run_cmd_and_verify


@pytest.mark.mtls
@pytest.mark.security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_gnmi_mtls_cli(test_api, gnmi_certs):
    """
    Verify that all CLI work and check values change properly in show

    1. Run show commands
    2. Verify outputs contain the required fields
    3. Set ca-certificate
    4. Verify in show commands
    5. Unset
    6. Verify in show commands
    """
    generic_test_mtls_cli(test_api, System().gnmi_server, [CA_CERTIFICATE], GNMI_INSTALLED, gnmi_certs)


@pytest.mark.mtls
@pytest.mark.security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_gnmi_mtls_set_bad_param(test_api, gnmi_certs):
    """
    Verify that set with bad param rejected

    1. Set api ca-certificate with bad param (CERT-ID or non existing/imported id)
    2. Verify command rejected
    3. Verify in show – expect no ca-cert installed to api
    """
    generic_test_mtls_set_bad_param(test_api, System().gnmi_server, gnmi_certs)


@pytest.mark.mtls
@pytest.mark.security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_gnmi_mtls_set_ca_without_cert_not_rejected(test_api, gnmi_certs, devices):
    """
    Verify that set api CA not rejected when no cert was previously set

    1. Set CA
    2. Verify command success
    3. Verify in show – expect ca to be installed to api
    """
    generic_test_mtls_set_ca_without_cert_not_rejected(test_api, System().gnmi_server, GNMI_INSTALLED, gnmi_certs)


@pytest.mark.mtls
@pytest.mark.security
@pytest.mark.parametrize('addressing_type', [AddressingType.IPV4, AddressingType.IPV6])
def test_gnmi_mtls_core_functionality(addressing_type, dut_ipv6_addr, gnmi_certs_no_import):
    generic_test_mtls_core_functionality(addressing_type, dut_ipv6_addr, System().gnmi_server, run_gnmi_client_and_verify,
                                         gnmi_certs_no_import, 20)


@pytest.mark.mtls
@pytest.mark.security
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
def test_gnmi_mtls_delete_installed_ca(test_flow, engines, local_adminuser, scp_player, gnmi_certs):
    """
    Verify that delete of ca-cert that is installed to api rejected

    1. Set api ca-certificate
    2. Try to delete that ca-certificate
    3. Verify reject
    4. Verify in show – expect ca-cert still installed
    5. Verify client cant request without suitable cert – expect fail
    """
    generic_test_mtls_delete_installed_ca(test_flow, engines, scp_player, local_adminuser, System().gnmi_server,
                                          GNMI_INSTALLED, run_gnmi_client_and_verify, False, gnmi_certs)


@pytest.mark.reboot
@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.mtls
@pytest.mark.security
@pytest.mark.parametrize('reboot_flow', RebootTestFlowType.ALL_TYPES)
def test_gnmi_mtls_reboot(reboot_flow, engines, gnmi_certs):
    """
    Verify mtls config and functionality after reboot

    1. Set api certificate & ca-certificate
    2. Save / no save
    3. Reboot
    4. Verify config in show
    5. Verify REST connection
    """
    generic_test_mtls_reboot(reboot_flow, engines, System().gnmi_server, GNMI_INSTALLED, run_gnmi_client_and_verify, False, gnmi_certs)


# gnmi mtls specific

def test_gnmi_mtls_session_persistence(engines, gnmi_certs):
    """
    Verify that an existing client session persists through change of gnmi TLS/mTLS  configuration, and vice versa

    Steps:
    - Initiate gnmi client session (to some port description)
    - Set port description “a”
    - Set TLS
    - set port description “b”
    - Set mTLS
    - Set port description “c”
    - Change CA cert
    - Set port description “d”
    - Unset gnmi config (including tls/mtls config)
    - Set port description “e”
    - Verify client received all port description changes
    - Verify client didn’t get any session down/certificate error message during the session
    """
    dut = engines.dut
    gnmi = System().gnmi_server
    cmd_runner = CmdRunner('gnmic', print_outputs=False)
    certs: List[CertInfo] = gnmi_certs
    client1_outputs = []
    client2_outputs = []

    with allure.step('randomize port to change description'):
        selected_port = RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
    with allure.step('Initiate gnmi client session'):
        client1_cmd = GnmicCmdBuilder(dut.ip).user_creds(dut.username, dut.password).subscribe_interface_description(selected_port.name).skip_verify().format_flat().debug().build()
        _, _, client1 = cmd_runner.run_cmd_in_process(client1_cmd, True)

    with allure.step('set port description'):
        client1_outputs.append(change_interface_description(selected_port))
    with allure.step('set TLS'):
        gnmi.set(CERTIFICATE, certs[0].name, apply=True).verify_result()
    with allure.step('set port description'):
        client1_outputs.append(change_interface_description(selected_port))
    with allure.step('set mTLS'):
        gnmi.mtls.set(CA_CERTIFICATE, certs[0].cacert_name, apply=True).verify_result()
        time.sleep(5)
    with allure.step('Initiate mTLS gnmi client session'):
        client2_cmd = GnmicCmdBuilder(dut.ip).user_creds(dut.username, dut.password).subscribe_interface_description(selected_port.name).cert(certs[0].private, certs[0].public).ca(certs[0].cacert).format_flat().debug().build()
        _, _, client2 = cmd_runner.run_cmd_in_process(client2_cmd, True)
    with allure.step('set port description'):
        client2_outputs.append(change_interface_description(selected_port))
    with allure.step('change CA'):
        gnmi.mtls.set(CA_CERTIFICATE, certs[1].cacert_name, apply=True).verify_result()
    with allure.step('set port description'):
        client2_outputs.append(change_interface_description(selected_port))
    with allure.step('unset gnmi'):
        gnmi.unset(apply=True).verify_result()
    with allure.step('set port description'):
        client2_outputs.append(change_interface_description(selected_port))
        client1_outputs.extend(client2_outputs)

    with allure.step("verify both clients received all data and didn’t get any session down/certificate error message during the session"):
        process, expected_outputs = 'process', 'expected-outputs'
        cases = {'client1 (unsecured)': {process: client1, expected_outputs: client1_outputs},
                 'client2 (mTLS)': {process: client2, expected_outputs: client2_outputs}}
        for case_name, case in cases.items():
            with allure.independent_step(case_name):
                with allure.independent_step('Verify client received all port description changes'):
                    out, err = cmd_runner.kill_cmd_process(case[process])
                    missing = [data for data in case[expected_outputs] if data not in out]
                    assert not missing, f'missing data in client output: {missing}'
                with allure.independent_step('Verify no session down/certificate error message during the session'):
                    found_errors = [error for error in GnmicErr.ALL_ERRS if error in f'{out}\n{err}']
                    assert not found_errors, f'{case_name} got errors: {found_errors}'
                if missing or found_errors:
                    allure.attach(f'stdout of {case_name}', out)
                    allure.attach(f'stderr of {case_name}', err)


def test_gnmi_mtls_stress(engines, gnmi_certs):
    """
    Verify that gnmi server is not blocked due to multiple clients with bad certificates

    Steps:
    - Set mtls
    - Subscribe multiple clients with bad certificates
    - Try another one with valid certificates – expect not denied
    """
    dut = engines.dut
    gnmi = System().gnmi_server
    cmd_runner = CmdRunner('gnmic', print_outputs=False)

    certs: List[CertInfo] = gnmi_certs
    server_cert: CertInfo = certs[0]
    server_ca: CertInfo = certs[1]
    other_cert: CertInfo = certs[2]
    host = server_cert.ip or server_cert.dn

    num_bad_clients = 10
    bad_clients: List[subprocess.Popen] = []

    with allure.step('randomize port to change description'):
        selected_port = RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
    with allure.step('set port description'):
        new_description = change_interface_description(selected_port)

    with allure.step('set mTLS'):
        gnmi.set(CERTIFICATE, server_cert.name).verify_result()
        gnmi.mtls.set(CA_CERTIFICATE, server_ca.cacert_name, apply=True).verify_result()

    try:
        with allure.step(f'Subscribe {num_bad_clients} clients with bad certificates'):
            bad_client_cmd = GnmicCmdBuilder(host).user_creds(dut.username, dut.password).subscribe_interface_description(
                selected_port.name).ca(other_cert.cacert).cert(other_cert.private, other_cert.public).format_flat().build()
            for i in range(num_bad_clients):
                _, _, p = cmd_runner.run_cmd_in_process(bad_client_cmd, True)
                bad_clients.append(p)
        with allure.step('Try another one with valid certificates – expect not denied'):
            good_client_cmd = GnmicCmdBuilder(host).user_creds(dut.username, dut.password).subscribe_interface_description(
                selected_port.name, GnmiMode.ONCE).ca(server_cert.cacert).cert(server_ca.private, server_ca.public).format_flat().debug().build()
            with allure.step('run cmd and verify not failing'):
                out = run_cmd_and_verify(good_client_cmd, server_cert, server_ca, True, 2 * MINUTE)
            with allure.step('verify got expected data'):
                assert new_description in out, f'good client did not get "{new_description}". output:\n{out}'
    finally:
        with allure.step('cleanup: kill bad client processes'):
            for p in bad_clients:
                cmd_runner.kill_cmd_process(p, kill_only=True)
            run_cmd('killall gnmic')


# generator functions

gnmi_mtls_factory_reset_no_params_check = generic_mtls_factory_reset_no_params_check(setup_gnmi_mtls_checker,
                                                                                     cleanup_gnmi_cert_tests,
                                                                                     System().gnmi_server,
                                                                                     GNMI_INSTALLED,
                                                                                     run_gnmi_client_and_verify, False)

gnmi_mtls_factory_reset_keep_all_config_check = generic_mtls_factory_reset_keep_all_config_check(setup_gnmi_mtls_checker,
                                                                                                 cleanup_gnmi_cert_tests,
                                                                                                 System().gnmi_server,
                                                                                                 GNMI_INSTALLED,
                                                                                                 run_gnmi_client_and_verify,
                                                                                                 False)

gnmi_mtls_upgrade_check = generic_mtls_upgrade_check(setup_gnmi_mtls_checker, cleanup_gnmi_cert_tests, System().gnmi_server,
                                                     GNMI_INSTALLED,
                                                     run_gnmi_client_and_verify, False)
