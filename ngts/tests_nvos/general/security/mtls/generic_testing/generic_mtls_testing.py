import random
import string
import time
from typing import List, Callable, Tuple

from typing_extensions import TypeAlias

import ngts.tools.test_utils.allure_utils as allure
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import TestFlowType, RebootTestFlowType, UserRole
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.system.MTLSableServerResource import MTLSableServerResource
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.certificate.helpers import import_test_certs
from ngts.tests_nvos.general.security.helpers import import_certs_safely, import_cas_safely
from ngts.tests_nvos.general.security.mtls.generic_testing.constants import MTLS, TEST_CERTS, CA_CERTIFICATE, \
    CERTIFICATE, Errors
from ngts.tests_nvos.general.security.mtls.generic_testing.helpers import verify_ca_configuration, \
    verify_mtls_config, verify_connection, VerifyConnFunc
from ngts.tests_nvos.general.security.security_test_tools.constants import AddressingType
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tests_nvos.system.gnmi.constants import GNMI_INSTALLED
from ngts.tests_nvos.system.gnmi.helpers import get_scp_player, wait_for_gnmi_ready
from ngts.ngts_types import EnginesT


def generic_test_mtls_cli(test_api, feature_resource: MTLSableServerResource, mtls_fields: List[str], installed_app_name: str, certs: List[CertInfo] = TEST_CERTS):
    """
    Verify that all CLI work and check values change properly in show

    1. Run show commands
    2. Verify outputs contain the required fields
    3. Set ca-certificate
    4. Verify in show commands
    5. Unset
    6. Verify in show commands
    """
    TestToolkit.tested_api = test_api

    imported_cas = [cert.cacert_name for cert in certs]
    ca = random.choice(imported_cas)

    with allure.step('run show commands and verify required fields'):
        with allure.independent_step(f'show: {feature_resource.get_resource_basename()}'):
            api_conf = OutputParsingTool.parse_json_str_to_dictionary(feature_resource.show()).get_returned_value()
            assert all(field in api_conf for field in [MTLS]), f'some of expected fields are missing from show api output\nexpected: {[MTLS]}\nout keys: {api_conf.keys()}'
        verify_ca_configuration(feature_resource, installed_app_name, None, mtls_fields)

    with allure.step(f'Set ca-certificate to {ca}'):
        feature_resource.mtls.set(CA_CERTIFICATE, ca, apply=True).verify_result()
    with allure.step('Verify in show commands'):
        verify_ca_configuration(feature_resource, installed_app_name, ca)

    with allure.step('check unset commands clear mtls config'):
        with allure.independent_step('check unset api'):
            with allure.step('run unset api'):
                feature_resource.unset(apply=True).verify_result()
            verify_ca_configuration(feature_resource, installed_app_name, None)
        with allure.independent_step('check unset mtls'):
            with allure.step(f'Set ca-certificate to {ca}'):
                feature_resource.mtls.set(CA_CERTIFICATE, ca, apply=True).verify_result()
            with allure.step('run unset mtls'):
                feature_resource.mtls.unset(apply=True).verify_result()
            verify_ca_configuration(feature_resource, installed_app_name, None)
        with allure.independent_step('check unset cacert field'):
            with allure.step(f'Set ca-certificate to {ca}'):
                feature_resource.mtls.set(CA_CERTIFICATE, ca, apply=True).verify_result()
            with allure.step('run unset mtls cacert'):
                feature_resource.mtls.unset(CA_CERTIFICATE, apply=True).verify_result()
            verify_ca_configuration(feature_resource, installed_app_name, None)


def generic_test_mtls_set_bad_param(test_api, feature_resource: MTLSableServerResource, certs: List[CertInfo] = TEST_CERTS):
    """
    Verify that set with bad param rejected

    1. Set api ca-certificate with bad param (CERT-ID or non existing/imported id)
    2. Verify command rejected
    3. Verify in show – expect no ca-cert installed to api
    """
    TestToolkit.tested_api = test_api

    rand_imported_cert: CertInfo = random.choice(certs)
    rand_str = ''.join(random.sample(string.ascii_lowercase, 6))
    bad_cas = [rand_str, rand_imported_cert.name]

    with allure.step('verify set with bad params rejected'):
        for bad_ca in bad_cas:
            with allure.independent_step(f'check bad param: {bad_ca}'):
                with allure.independent_step('check command is rejected'):
                    res = feature_resource.mtls.set(CA_CERTIFICATE, bad_ca, apply=True)
                    res.verify_result(False)
                    expected_err = Errors.CERT_DONT_EXIST.format(cert_id=bad_ca)
                    assert expected_err in res.info, f'error message not as expected\nexpected: "{expected_err}"\nactual: {res.info}'
                with allure.independent_step('check no change in show mtls'):
                    verify_mtls_config(feature_resource)


def generic_test_mtls_set_ca_without_cert_not_rejected(test_api, feature_resource: MTLSableServerResource, installed_app_name: str, certs: List[CertInfo] = TEST_CERTS):
    """
    Verify that set api CA not rejected when no cert was previously set

    1. Set CA
    2. Verify command success
    3. Verify in show – expect ca to be installed to api
    """
    TestToolkit.tested_api = test_api

    imported_cas = [cert.cacert_name for cert in certs]
    ca = random.choice(imported_cas)

    with allure.step(f'set CA: {ca} without setting cert explicitly, expect success'):
        feature_resource.mtls.set(CA_CERTIFICATE, ca, apply=True).verify_result()
    with allure.step('verify in show'):
        verify_ca_configuration(feature_resource, installed_app_name, ca)


def generic_test_mtls_core_functionality(addressing_type: str, dut_ipv6_addr: str,
                                         feature_resource: MTLSableServerResource, verify_connection_func: VerifyConnFunc,
                                         certs: List[CertInfo], connection_cmd_timeout=None):
    """
    Verify the core functionality:

    after setting ca-certificate, client can request only using suitable cert & ca (mtls)

    1. Bind cert & ca-cert to api
    2. Send client request using proper cert & cacert – expect success
    3. Send client request using non-proper cert/cacert – expect failure
    4. Unset ca-cert
    5. Send client request using any/no cert – expect success (server mtls off)
    """
    engines: EnginesT = TestToolkit.engines
    scp_player = get_scp_player(engines)

    dut = engines.dut
    host = dut_ipv6_addr if addressing_type == AddressingType.IPV6 else dut.ip
    bad_host = '1.2.3.4'

    user = UserInfo('admin', 'admin', 'admin')
    bad_creds = UserInfo('admin', 'adm', 'admin')

    cert1: CertInfo = certs[0]
    cert2: CertInfo = certs[1]
    cert3: CertInfo = certs[2]

    with allure.step('import test certs'):
        import_certs_safely([cert1], scp_player)
        import_cas_safely([cert2], scp_player)

    class Case:
        # static state
        configured_server_cert = None
        configured_server_ca = None

        def __init__(self, name: str, host: str, user: UserInfo, expect: bool, unsecured: bool,
                     client_cert: CertInfo = None, client_ca: CertInfo = None,
                     server_cert: CertInfo = None, server_ca: CertInfo = None):
            self.name: str = name
            self.host: str = host
            self.user: UserInfo = user
            self.expect: bool = expect
            self.unsecured: bool = unsecured
            self.client_cert: CertInfo = client_cert
            self.client_ca: CertInfo = client_ca
            self.server_cert: CertInfo = server_cert
            self.server_ca: CertInfo = server_ca

        def run(self):
            with allure.step('setup'):
                should_apply = False
                cert_changed = False
                ca_changed = False
                if self.server_cert and self.server_cert.name != Case.configured_server_cert:
                    feature_resource.set(CERTIFICATE, self.server_cert.name).verify_result()
                    should_apply = True
                    cert_changed = True
                elif self.server_cert is None and Case.configured_server_cert is not None:
                    feature_resource.unset(CERTIFICATE).verify_result()
                    cert_changed = True
                    should_apply = True
                if self.server_ca and self.server_ca.cacert_name != Case.configured_server_ca:
                    feature_resource.mtls.set(CA_CERTIFICATE, self.server_ca.cacert_name).verify_result()
                    ca_changed = True
                    should_apply = True
                elif self.server_ca is None and Case.configured_server_ca is not None:
                    feature_resource.mtls.unset().verify_result()
                    ca_changed = True
                    should_apply = True
                if should_apply:
                    feature_resource._general_cli_wrapper.apply_config(dut)
                    if cert_changed and ca_changed:
                        sleep_time = 10
                        with allure.step(f"When changing both cert and ca-cert gnmi rotation takes {sleep_time} sec"):
                            time.sleep(sleep_time)
                Case.configured_server_cert = self.server_cert.name if self.server_cert else None
                Case.configured_server_ca = self.server_ca.cacert_name if self.server_ca else None
            with allure.step('verify client'):
                verify_connection_func(self.host, self.user, self.expect, self.unsecured, self.client_ca, self.client_cert, connection_cmd_timeout)

    cases: List[Case] = [
        Case('simple good flow', host, user, True, True),
        Case('bad host', bad_host, user, False, True),
        Case('bad creds', host, bad_creds, False, True),
        # tls cases
        Case('server with cert, client with proper ca', host, user, True, False, server_cert=cert1, client_ca=cert1),
        Case('server with cert, client unsecured', host, user, True, True, server_cert=cert1),
        Case('server with cert, client with non-proper ca', host, user, False, False, server_cert=cert1, client_ca=cert3),
        Case('server with no cert (self-signed), client with some ca (requires TLS)', host, user, False, False, client_ca=cert1),
        # mtls cases
        Case('server mtls, client with good cert + ca', host, user, True, False, cert2, cert1, cert1, cert2),
        Case('server mtls, client with good cert + bad ca', host, user, False, False, cert2, cert3, cert1, cert2),
        Case('server mtls, client with bad cert + good ca', host, user, False, False, cert3, cert1, cert1, cert2),
        Case('server mtls, client unsecured', host, user, False, True, None, None, cert1, cert2),
        Case('server mtls, client tls', host, user, False, False, None, cert1, cert1, cert2),
    ]

    with allure.step('run all cases'):
        for case in cases:
            with allure.independent_step(f'[{"good flow" if case.expect else "bad flow"}] {case.name}'):
                case.run()


def generic_test_mtls_delete_installed_ca(test_flow: str, engines, scp_player: LinuxSshEngine, local_adminuser: UserInfo,
                                          feature_resource: MTLSableServerResource, installed_app_name: str,
                                          verify_connection_func: VerifyConnFunc, non_matching_client_cert_should_work: bool,
                                          certs: List[CertInfo] = TEST_CERTS):
    """
    Verify that delete of ca-cert that is installed to api rejected

    1. Set api ca-certificate
    2. Try to delete that ca-certificate
    3. Verify reject
    4. Verify in show – expect ca-cert still installed
    5. Verify client cant request without suitable cert – expect fail
    """
    server_cert: CertInfo = random.choice(certs)
    server_ca: CertInfo = RandomizationTool.select_random_value(certs, [server_cert]).get_returned_value()

    try:
        with allure.step(f'set some cert: {server_cert.name}'):
            feature_resource.set(CERTIFICATE, server_cert.name).verify_result()
        with allure.step(f'set ca: {server_ca.cacert_name}'):
            feature_resource.mtls.set(CA_CERTIFICATE, server_ca.cacert_name, apply=True).verify_result()
        with allure.step(f'try delete ca "{server_ca.cacert_name}". expect fail'):
            res = System().security.ca_certificate.cert_id[server_ca.cacert_name].action_delete()
            res.verify_result(False)
            expected_err = Errors.INSTALLED_CA_DELETE_ERR.format(ca_id=server_ca.cacert_name, app_name=installed_app_name)
            assert expected_err in res.info, f'error message not as expected\nexpected: "{expected_err}"\nactual: {res.info}'
        with allure.step('verify no change show'):
            verify_ca_configuration(feature_resource, installed_app_name, server_ca.cacert_name)
        with allure.step('verify api server is still mtls only'):
            verify_connection(test_flow, engines.dut, local_adminuser, True, server_cert, server_ca,
                              non_matching_client_cert_should_work, verify_connection_func, test_certs=certs)
    finally:
        with allure.step('cleanup: import test certs back'):
            import_test_certs(scp_player, engines.dut, certs)


def generic_test_mtls_reboot(reboot_flow: str, engines, feature_resource: MTLSableServerResource,
                             installed_app_name: str, verify_connection_func: VerifyConnFunc,
                             non_matching_client_cert_should_work: bool, certs: List[CertInfo] = TEST_CERTS):
    """
    Verify mtls config and functionality after reboot

    1. Set api certificate & ca-certificate
    2. Save / no save
    3. Reboot
    4. Verify config in show
    5. Verify REST connection
    """
    with_save = reboot_flow == RebootTestFlowType.WITH_SAVE
    server_cert: CertInfo = certs[0]
    server_ca: CertInfo = certs[1]
    user = UserInfo(engines.dut.username, engines.dut.password, UserRole.ADMIN)

    with allure.step(f'set some cert: {server_cert.name}'):
        feature_resource.set(CERTIFICATE, server_cert.name).verify_result()
    with allure.step(f'set ca: {server_ca.cacert_name}'):
        feature_resource.mtls.set(CA_CERTIFICATE, server_ca.cacert_name, apply=True).verify_result()

    if with_save:
        with allure.step('save config'):
            NvueGeneralCli.save_config(engines.dut)

    with allure.step('reboot the system'):
        System().action_reboot('force').verify_result()

    with allure.step('verify show'):
        verify_ca_configuration(feature_resource, installed_app_name, server_ca.cacert_name if with_save else None)

    if installed_app_name == GNMI_INSTALLED:
        wait_for_gnmi_ready(engines)

    with allure.step(f'verify {"no " if not with_save else ""} mtls only connection'):
        verify_connection(TestFlowType.ALL_TYPES, engines.dut, user, with_save, server_cert, server_ca,
                          non_matching_client_cert_should_work, verify_connection_func, test_certs=certs)


# generator functions

SetupFunc: TypeAlias = Callable[[], Tuple[str, CertInfo, CertInfo]]
CleanupFunc: TypeAlias = Callable[[str, List[CertInfo], List[CertInfo]], None]


def generic_mtls_factory_reset_no_params_check(setup_steps: SetupFunc, cleanup_steps: CleanupFunc,
                                               feature_resource: MTLSableServerResource,
                                               installed_app_name: str,
                                               verify_connection_func: VerifyConnFunc,
                                               non_matching_client_cert_should_work: bool):
    """
    Verify that mtls config is removed after these reset factory flavors

    The same for these flavors:
    - regular/no params (remove everything)
    - keep basic (remove everything except for some basic configuration)

    1. Set api mtls
    2. Save
    3. Do factory reset
    4. Verify no mtls configuration in show
    5. Verify no mtls connection
    """
    def checker():
        engines = TestToolkit.engines

        with allure.step('setup'):
            tmp_certs_dir, server_cert, server_ca = setup_steps()

        yield  # factory reset

        try:
            with allure.step('verify after factory reset'):
                with allure.step('verify no mtls after this factory reset'):
                    with allure.independent_step('verify mtls is not configured in show'):
                        verify_mtls_config(feature_resource)
                    if installed_app_name == GNMI_INSTALLED:
                        wait_for_gnmi_ready(engines)
                    with allure.independent_step('verify no mtls only connection'):
                        verify_connection(TestFlowType.ALL_TYPES, engines.dut, UserInfo(engines.dut.username, engines.dut.password, 'admin'),
                                          False, server_cert, server_ca, non_matching_client_cert_should_work, verify_connection_func)
        finally:
            with allure.step('cleanup'):
                cleanup_steps(tmp_certs_dir, [server_cert], [server_ca])

        yield  # to prevent StopIteration on the 2nd next() call

    return checker


def generic_mtls_factory_reset_keep_all_config_check(setup_steps: SetupFunc, cleanup_steps: CleanupFunc,
                                                     feature_resource: MTLSableServerResource, installed_app_name: str,
                                                     verify_connection_func: VerifyConnFunc,
                                                     non_matching_client_cert_should_work: bool):
    """
    Verify that mtls config is kept after this factory reset flavor

    Keep all config – removes everything except for saved configuration
    * when saved config includes ca/cert, the config should be kept and also the ca/cert files themselves.

    1. Set api mtls
    2. Save
    3. Do factory reset
    4. Verify mtls config in show
    5. Verify mtls connection only
    """

    def checker():
        engines = TestToolkit.engines

        with allure.step('setup'):
            tmp_certs_dir, server_cert, server_ca = setup_steps()

        yield  # factory reset

        try:
            with allure.step('verify mtls after this factory reset'):
                with allure.independent_step('verify mtls is configured in show'):
                    verify_ca_configuration(feature_resource, installed_app_name, server_ca.cacert_name)
                if installed_app_name == GNMI_INSTALLED:
                    wait_for_gnmi_ready(engines)
                with allure.independent_step('verify mtls only connection'):
                    verify_connection(TestFlowType.ALL_TYPES, engines.dut, UserInfo(engines.dut.username, engines.dut.password, 'admin'),
                                      True, server_cert, server_ca, non_matching_client_cert_should_work, verify_connection_func)
        finally:
            cleanup_steps(tmp_certs_dir, [server_cert], [server_ca])

        yield  # to prevent StopIteration on the 2nd next() call

    return checker


generic_mtls_upgrade_check = generic_mtls_factory_reset_keep_all_config_check
