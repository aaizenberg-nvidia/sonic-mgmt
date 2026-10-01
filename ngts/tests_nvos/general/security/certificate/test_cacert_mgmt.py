import random
from typing import List

import pytest

from ngts.cli_wrappers.openapi.openapi_command_builder import OpenApiRequest
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.bmc.bmc_erot_attestation.helpers import randomize_hex_str
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.certificate.conftest import clear_existing_certs
from ngts.tests_nvos.general.security.certificate.constants import TestCert, CaShowFields, CA_TYPE_EXTERNAL, \
    CA_TYPE_GLOBAL
from ngts.tests_nvos.general.security.certificate.helpers import verify_ca_in_expected_locations, \
    send_curl_with_and_verify
from ngts.tests_nvos.general.security.nmx_cert.constants import EncryptionMode
from ngts.tests_nvos.general.security.test_api_server_security.constants import CERTIFICATE, CA_CERTIFICATE
from ngts.tests_nvos.system.gnmi.conftest import scp_player
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_general_utils import generate_scp_uri_using_player


@pytest.mark.nvos_ci
@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cacert_mgmt_cacert_cli(test_api, engines, scp_player, clear_certs):
    """
    Verify that all CLI work and check values change properly in show

    1.  Show cas – expect no data – {}
    2.  Import ca1 using data
    3.  Import ca2 using URI
    4.  One of the imports should be external
    5.  Show cas – expect cert1,2 in output
    6.  Verify files in expected locations
    7.  Verify global ca content is in CAs pool file, and external is not
    8.  Show a global ca and an external ca – expect fields [count, installed, serial-number, valid-from, valid-to, type]
    9.  Verify count = 1
    10. Verify installed empty {}
    11. Verify all the rest non empty strings
    12. Verify type – ‘external’ if imported with ‘external_ca’; ‘global’ otherwise
    13. Show dump of any cert – expect ‘CN = Root CA’ & ‘CA:TRUE’ in output (sanity)
    14. Show installed of any cert – expect empty {}
    15. Delete a ca
    16. Show Cas – expect deleted ca not exist
    17. Verify no files at the expected locations
    """
    TestToolkit.tested_api = test_api

    security = System().security

    ca1_is_external = random.choice([True, False])
    ca1 = TestCert.cert_valid_1.copy(f'ca1-{"external" if ca1_is_external else "global"}')
    ca2 = TestCert.cert_valid_2.copy(f'ca2-{"external" if not ca1_is_external else "global"}')

    external_info = {
        ca1.name: ca1_is_external,
        ca2.name: not ca1_is_external
    }

    cas: List[CertInfo] = [ca1, ca2]

    with allure.step('Show cas – expect no data – {}'):
        out = OutputParsingTool.parse_json_str_to_dictionary(security.ca_certificate.show()).get_returned_value()
        assert not out, f'show ca expected to be empty but it is not\n{out}'
    with allure.step('import cas (one default/global and other external)'):
        with allure.independent_step(f'Import {ca1.name} using data'):
            with allure.step('get cert data as string'):
                data = ca1.get_ca_content_str()
            with allure.step('import with data'):
                security.ca_certificate.cert_id[ca1.name].action_import(data=data, external=external_info[ca1.name]).verify_result()
        with allure.independent_step(f'Import {ca2.name} using URI'):
            uri = generate_scp_uri_using_player(scp_player, ca2.cacert)
            security.ca_certificate.cert_id[ca2.name].action_import(uri=uri, external=external_info[ca2.name]).verify_result()
    with allure.step('Show cas – expect all imported cas in output'):
        out = OutputParsingTool.parse_json_str_to_dictionary(security.ca_certificate.show()).get_returned_value()
        assert all(ca.name in out for ca in cas), f'not all expected cas names in show output\nexpected: {[ca.name for ca in cas]}\nout:\n{out}'
    with allure.step('Verify files in expected locations'):
        for ca in cas:
            with allure.independent_step(f'verify "{ca.name}" in expected cert locations'):
                verify_ca_in_expected_locations(ca.name, ca, engines.dut, external_info[ca.name])
    rand_global_ca: CertInfo = random.choice([ca for ca in cas if external_info[ca.name] == False])
    rand_external_ca: CertInfo = random.choice([ca for ca in cas if external_info[ca.name]])
    with allure.step('continue flow with rand global & external CA'):
        for rand_ca in [rand_global_ca, rand_external_ca]:
            with allure.independent_step(f'continue checks with {rand_ca.name}'):
                with allure.independent_step(f'Show a single cert "{rand_ca.name}" – expect fields {CaShowFields.ALL_FIELDS}'):
                    out_single = OutputParsingTool.parse_json_str_to_dictionary(security.ca_certificate.cert_id[rand_ca.name].show()).get_returned_value()
                    assert all(field in out_single for field in CaShowFields.ALL_FIELDS if field != CaShowFields.INSTALLED), f'not all expected fields in single ca show output\nexpected: {CaShowFields.ALL_FIELDS}\nout:\n{out}'
                with allure.independent_step('verify show values'):
                    for field in CaShowFields.ALL_FIELDS:
                        if field == CaShowFields.TYPE:
                            expected_type = CA_TYPE_EXTERNAL if external_info[rand_ca.name] else CA_TYPE_GLOBAL
                            with allure.independent_step(f'Verify type is {expected_type}'):
                                assert str(out_single[field]) == expected_type, f'field {field} not as expected\nexpected: {expected_type}\nactual: {out_single[field]}'
                        elif field == CaShowFields.COUNT:
                            with allure.independent_step('Verify count is 1'):
                                assert str(out_single[field]) == '1', f'field {field} not as expected\nexpected: 1\nactual: {out_single[field]}'
                        elif field == CaShowFields.INSTALLED:
                            with allure.independent_step('Verify installed empty {}'):
                                install_val = out_single.get(field, '')
                                assert not install_val, f'field {field} not as expected\nexpected: {"{}"}\nactual: {install_val}'
                        else:
                            with allure.independent_step(f'verify {field} not empty'):
                                assert out_single[field] != '', f'field {field} not as expected\nexpected: not empty\nactual: ""'
                with allure.independent_step('verify inner show commands on ca'):
                    expected_patterns_in_content = ['CN = Root CA', 'CA:TRUE']
                    with allure.independent_step(f'Show dump of any cert – expect "{expected_patterns_in_content}" in output (sanity)'):
                        dump_out_str = security.ca_certificate.cert_id[rand_ca.name].dump.show()
                        assert all(pattern in dump_out_str for pattern in expected_patterns_in_content), f'not all of {expected_patterns_in_content} found in show dump output of {rand_ca.name}\nout:\n{dump_out_str}'
                with allure.step(f'Delete a ca: {rand_ca.name}'):
                    security.ca_certificate.cert_id[rand_ca.name].action_delete().verify_result()
                with allure.independent_step('Show all cas – expect deleted ca not exist'):
                    out = OutputParsingTool.parse_json_str_to_dictionary(security.ca_certificate.show()).get_returned_value()
                    assert rand_ca.name not in out, f'deleted ca {rand_ca.name} unexpectedly exists in show output'
                with allure.independent_step(f'Verify deleted ca {rand_ca.name} not exists in expected locations'):
                    verify_ca_in_expected_locations(rand_ca.name, rand_ca, engines.dut, should_exist=False)


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cacert_mgmt_import_ca_data_bad_param(test_api, engines, scp_player, clear_certs):
    """
    Verify that import cert with bad params rejected

    1. Empty
    2. random short string
    3. real cert that injected letter in the middle
    """
    TestToolkit.tested_api = test_api

    security = System().security

    ca = TestCert.cert_valid_1.copy()
    real_data = ca.get_ca_content_str()

    ca_name, ca_obj, data, external = 'ca_name', 'ca_obj', 'data', 'external'
    cases = [
        {ca_name: 'ca-empty-string-global', ca_obj: ca, data: '', external: False},
        {ca_name: 'ca-rand-string-global', ca_obj: ca, data: randomize_hex_str(10), external: False},
        {ca_name: 'ca-messed-data-global', ca_obj: ca, data: real_data[:50 - 5] + 'ALON' + real_data[50 - 1:], external: False},
    ]

    # make one of them external
    rand_ca_idx = random.randint(0, len(cases) - 1)
    cases[rand_ca_idx][external] = True
    cases[rand_ca_idx][ca_name] = cases[rand_ca_idx][ca_name].replace('global', external)

    with allure.step('import cas using bad data params - expect fail and not in output'):
        with allure.independent_step('try import ca with using data param with bad values'):
            for case in cases:
                with allure.independent_step(case[ca_name]):
                    security.ca_certificate.cert_id[case[ca_name]].action_import(data=case[data], external=case[external]).verify_result(False)
        with allure.independent_step('verify no ca in show'):
            out = OutputParsingTool.parse_json_str_to_dictionary(security.ca_certificate.show()).get_returned_value()
            assert out == {}, f'output not as expected\nexpected: {"{}"}\nactual: {out}'
        with allure.independent_step('Verify cas not exist in expected locations'):
            for case in cases:
                with allure.independent_step(case[ca_name]):
                    verify_ca_in_expected_locations(case[ca_name], case[ca_obj], engines.dut, case[external], False)


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cacert_mgmt_import_ca_uri_bad_param(test_api, engines, scp_player, clear_certs):
    """
    Verify that import ca with bad params rejected

    1. empty values
    2. random string as url
    """
    TestToolkit.tested_api = test_api

    security = System().security

    name, description, uri, external = 'name', 'description', 'uri', 'external'
    cases = [
        {name: 'ca1-global', description: 'empty uri', uri: "", external: False},
        {name: 'ca2-global', description: 'random str uri', uri: 'xyz', external: False}
    ]

    # make one of them external
    rand_ca_idx = random.randint(0, len(cases) - 1)
    cases[rand_ca_idx][external] = True
    cases[rand_ca_idx][name] = cases[rand_ca_idx][name].replace('global', external)

    with allure.step('import certs using bad data params - expect fail and not in output'):
        with allure.independent_step('try import cert with using data param with bad values'):
            for case in cases:
                with allure.independent_step(f'{case[name]} - {case[description]}'):
                    security.ca_certificate.cert_id[case[name]].action_import(uri=case[uri], external=case[external]).verify_result(False)
        with allure.independent_step('verify no ca in show'):
            out = OutputParsingTool.parse_json_str_to_dictionary(security.ca_certificate.show()).get_returned_value()
            assert out == {}, f'output not as expected\nexpected: {"{}"}\nactual: {out}'
        with allure.independent_step('Verify cas not exist in expected locations'):
            for case in cases:
                with allure.independent_step(case[name]):
                    verify_ca_in_expected_locations(case[name], None, engines.dut, case[external], False)


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cacert_mgmt_delete_ca_bad_param(test_api, engines, scp_player, clear_certs):
    """
    Verify that delete ca with bad params rejected and doesn't affect other imported cas
    """
    TestToolkit.tested_api = test_api

    security = System().security

    name, ca_info, external = 'name', 'ca_info', 'external'
    cas = [
        {name: f'ca{i + 1}-global', ca_info: TestCert.cert_valid_1.copy(f'ca{i + 1}-global'), external: False} for i in range(5)
    ]
    uri = generate_scp_uri_using_player(scp_player, TestCert.cert_valid_1.cacert)

    # make one of them external
    rand_ca_idx = random.randint(0, len(cas) - 1)
    new_name = f'ca{rand_ca_idx + 1}-{external}'
    cas[rand_ca_idx][external] = True
    cas[rand_ca_idx][name] = new_name
    cas[rand_ca_idx][ca_info] = TestCert.cert_valid_2.copy(new_name)

    with allure.step('import some cas'):
        for ca in cas:
            with allure.independent_step(ca[name]):
                security.ca_certificate.cert_id[ca[name]].action_import(uri=uri, external=ca[external]).verify_result()
    with allure.step('delete cert that does not exist'):
        security.ca_certificate.cert_id['xyz'].action_delete().verify_result(False)
    with allure.step('verify all imported certs still exist'):
        out = OutputParsingTool.parse_json_str_to_dictionary(security.ca_certificate.show()).get_returned_value()
        for ca in cas:
            with allure.independent_step(ca[name]):
                with allure.independent_step('verify exists in show'):
                    assert ca[name] in out, f'{ca[name]} does not exist in show ca-certificate output, but expected to exist\n{out}'
                with allure.independent_step('verify exists in expected locations'):
                    verify_ca_in_expected_locations(ca[name], ca[ca_info], engines.dut, ca[external])


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cacert_mgmt_import_ca_unique_id(test_api, engines, scp_player, clear_certs):
    """
    Verify that must use unique ca id

    1. import ca1
    2. import another ca with same id – expect rejected
    """
    TestToolkit.tested_api = test_api

    security = System().security
    cacert_global = TestCert.cert_valid_1
    uri_global = generate_scp_uri_using_player(scp_player, cacert_global.cacert)
    cacert_external = TestCert.cert_valid_2
    uri_external = generate_scp_uri_using_player(scp_player, cacert_external.cacert)

    title, ca_name, uri, expect, external, ca_info = 'title', 'ca_name', 'uri', 'expect', 'external', 'ca_info'
    cases: List[dict] = [
        {title: 'import ca1 (global)', ca_name: 'ca1', ca_info: cacert_global, uri: uri_global, external: False, expect: True},
        {title: 'import ca2 (global)', ca_name: 'ca2', ca_info: cacert_global, uri: uri_global, external: False, expect: True},
        {title: 'repeat ca1 (global)', ca_name: 'ca1', ca_info: cacert_global, uri: uri_global, external: False, expect: False},
        {title: 'import ca3 (global)', ca_name: 'ca3', ca_info: cacert_global, uri: uri_global, external: False, expect: True},
        {title: 'repeat ca2 (global)', ca_name: 'ca2', ca_info: cacert_global, uri: uri_global, external: False, expect: False},
        {title: 'import ca4 (global)', ca_name: 'ca4', ca_info: cacert_global, uri: uri_global, external: False, expect: True},
    ]

    # make one of them external
    rand_ca_idx = random.randint(0, len(cases) - 1)
    cases[rand_ca_idx][title] = cases[rand_ca_idx][title].replace('global', external)
    cases[rand_ca_idx][external] = True
    cases[rand_ca_idx][uri] = uri_external
    cases[rand_ca_idx][ca_info] = cacert_external

    with allure.step('test importing in several cases'):
        for case in cases:
            with allure.independent_step(f'{case[title]} - expect: {case[expect]}'):
                security.ca_certificate.cert_id[case[ca_name]].action_import(uri=case[uri], external=case[external]).verify_result(case[expect])
    with allure.step('show cas'):
        out = OutputParsingTool.parse_json_str_to_dictionary(security.ca_certificate.show()).get_returned_value()
    with allure.step('verify existence of all cas'):
        for case in [c for c in cases if c[expect]]:
            with allure.independent_step(case[title]):
                with allure.independent_step('verify in show'):
                    assert case[ca_name] in out, f'ca {case[ca_name]} does not appear in ca-certificate show output but expected to exist\n{out}'
                with allure.independent_step('verify files'):
                    verify_ca_in_expected_locations(case[ca_name], case[ca_info], engines.dut, case[external])


""" functional tests """


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', random.sample([ApiType.NVUE], 1))
def test_cert_mgmt_use_ca_for_rest_api_mtls(test_api, engines, scp_player, clear_certs):
    """
    Verify that valid imported cert can be used for REST server TLS

    1. import cert
    2. bind cert to rest server (system api certificate)
    3. send unsecured client request – success
    4. send client request using non/proper CA - fail/success
    """
    TestToolkit.tested_api = test_api

    system = System()
    security = system.security

    server_cert = TestCert.cert_valid_1.copy('cert1')
    cert_bundle_uri = generate_scp_uri_using_player(scp_player, server_cert.p12_bundle)

    cert1, cert2, cert3 = TestCert.cert_valid_1.copy('cert1'), TestCert.cert_valid_2.copy('cert2'), TestCert.cert_valid_3.copy('cert3')

    class CA:
        def __init__(self, obj: CertInfo, external: bool = False):
            self.obj: CertInfo = obj
            self.external: bool = external

    class Cert:
        def __init__(self, obj: CertInfo, expect: bool):
            self.obj: CertInfo = obj
            self.expect: bool = expect

    class Case:
        def __init__(self, title: str, server_cas: List[CA], bind: CertInfo, client_certs: List[Cert], bind_external: bool = False):
            self.title: str = title
            self.server_cas: List[CA] = server_cas
            self.bind: CertInfo = bind
            self.client_certs: List[Cert] = client_certs
            self.bind_external: bool = bind_external

    cases = [
        Case('import + bind global ca', [CA(cert2)], cert2, [Cert(cert2, True), Cert(cert3, False)]),
        Case('import 2 globals (bind one) - both should work', [CA(cert2), CA(cert3)], cert2, [Cert(cert2, True), Cert(cert3, True)]),
        Case('import global + external, bind global - only global should work', [CA(cert2), CA(cert3, True)], cert2, [Cert(cert2, True), Cert(cert3, False)]),
    ]

    with allure.step('test with several cases'):
        for case in cases:
            if case.bind_external:
                continue  # can't bind external ca to api
            with allure.independent_step(f'case: {case.title}'):
                with allure.step('clear existing ca/certs'):
                    TestToolkit.tested_api = ApiType.NVUE
                    system.api.unset(apply=True)
                    OpenApiRequest.update_client_certs_info(None)
                    clear_existing_certs(engines)
                    TestToolkit.tested_api = test_api
                with allure.step('import server cert'):
                    security.certificate.cert_id[server_cert.name].action_import(uri_bundle=cert_bundle_uri, passphrase=server_cert.p12_password).verify_result()
                with allure.step('import server cas'):
                    for ca_info in case.server_cas:
                        ca = ca_info.obj
                        extr = ca_info.external
                        security.ca_certificate.cert_id[ca.cacert_name].action_import(uri=generate_scp_uri_using_player(scp_player, ca.cacert), external=extr).verify_result()
                if case.bind:
                    with allure.step(f'bind server ca: {case.bind.cacert_name}'):
                        system.api.mtls.set(CA_CERTIFICATE, case.bind.cacert_name)
                with allure.step('bind server cert and apply'):
                    system.api.set(CERTIFICATE, server_cert.name, apply=True, client_certs_after_apply=CertInfo('', '', case.bind.private, case.bind.public, '', '', case.bind.dn, None, '')).verify_result()
                with allure.independent_step('send unsecured client request – expect fail'):
                    send_curl_with_and_verify(server_cert.dn, engines.dut.username, engines.dut.password, EncryptionMode.DISABLED, should_succeed=False)
                for client_cert in case.client_certs:
                    with allure.independent_step(f'send client request using {"" if client_cert.expect else "non-"}proper client cert ({client_cert.obj.name}) – expect {"success" if client_cert.expect else "fail"}'):
                        client_ca = server_cert  # client cert matches server CA
                        send_curl_with_and_verify(server_cert.dn, engines.dut.username, engines.dut.password, EncryptionMode.MTLS, client_ca, client_cert.obj, client_cert.expect)
