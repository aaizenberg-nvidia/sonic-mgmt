import os
import random
import tempfile
from typing import List

import pytest

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_constants.constants_nvos import ApiType, TestFlowType
from ngts.nvos_tools.infra.CertificateGenerator import CertificateGeneratorOnRemoteHost
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OpenSslCmdBuilder import OpenSslCmdBuilder
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.bmc.bmc_erot_attestation.helpers import randomize_hex_str
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.certificate.conftest import clear_certs, clear_existing_certs
from ngts.tests_nvos.general.security.certificate.constants import TEST_CERTS, TestCert, CertShowFields, DUT_IMPORTED_CERTS_PUBLIC_DIR
from ngts.tests_nvos.general.security.certificate.helpers import verify_cert_in_expected_locations, import_certificates, \
    send_curl_with_and_verify
from ngts.tests_nvos.general.security.nmx_cert.constants import EncryptionMode
from ngts.tests_nvos.general.security.test_api_server_security.constants import CERTIFICATE
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.system.gnmi.conftest import scp_player
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_general_utils import generate_file_location_uri, generate_scp_uri_using_player
from tests.platform_tests.test_first_time_boot_password_change.conftest import dut_hostname
from ngts.nvos_tools.infra.CmdRunner import CmdRunner

""" CLI tests """


@pytest.mark.nvos_ci
@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cert_mgmt_cert_cli(test_api, engines, scp_player, clear_certs):
    """
    Verify that all CLI work and check values change properly in show

    1.	Show certs – expect no data – {}
    2.	Import cert1 using data
    3.	Import cert2 using private & public key URIs
    4.	Import cert3 using bundle URI + no pass (use empty string as value or without specifying pass)
    5.	Import cert4 using bundle URI + regular pass
    6.	Import cert5 using bundle URI + long pass (customer bug)
    7.	Show certs – expect cert1,2,3,4,5 in output
    8.	Verify files in expected locations
    9.	Show a single cert – expect fields [installed, serial-number, valid-from, valid-to]
    10.	Verify installed empty {}
    11.	Verify all the rest non empty strings
    12.	Show dump of any cert – expect 'DNS:nvos-dut' in output (sanity)
    13.	Show installed of any cert – expect empty {}
    14.	Delete a cert
    15.	Show all certs – expect deleted cert not exist
    """
    TestToolkit.tested_api = test_api

    security = System().security

    cert1 = TestCert.cert_valid_1.copy('cert1')
    cert2 = TestCert.cert_valid_1.copy('cert2')
    cert4 = TestCert.cert_valid_1_no_passphrase.copy('cert4')
    cert5 = TestCert.cert_valid_1.copy('cert5')
    cert6 = TestCert.cert_valid_1_long_passphrase.copy('cert6')

    certs: List[CertInfo] = []

    with allure.step('Show certs – expect no data – {}'):
        out = OutputParsingTool.parse_json_str_to_dictionary(security.certificate.show()).get_returned_value()
        assert not out, f'show cert expected to be empty but it is not\n{out}'
    with allure.step('import certs'):
        with allure.independent_step('Import cert1 using data'):
            with allure.step('get cert data as string'):
                data = cert1.get_cert_content_str()
            with allure.step('import with data'):
                security.certificate.cert_id[cert1.name].action_import(data=data).verify_result()
                certs.append(cert1)
        with allure.independent_step('Import cert2 using private & public key URIs'):
            private_uri = generate_scp_uri_using_player(scp_player, cert2.private)
            public_uri = generate_scp_uri_using_player(scp_player, cert2.public)
            security.certificate.cert_id[cert2.name].action_import(uri_private_key=private_uri,
                                                                   uri_public_key=public_uri).verify_result()
            certs.append(cert2)
        with allure.independent_step('Import cert4 using bundle URI + no pass (without specifying pass param)'):
            bundle_uri = generate_scp_uri_using_player(scp_player, cert4.p12_bundle)
            security.certificate.cert_id[cert4.name].action_import(uri_bundle=bundle_uri).verify_result()
            certs.append(cert4)
        with allure.independent_step('Import cert5 using bundle URI + regular pass'):
            bundle_uri = generate_scp_uri_using_player(scp_player, cert5.p12_bundle)
            security.certificate.cert_id[cert5.name].action_import(uri_bundle=bundle_uri,
                                                                   passphrase=cert5.p12_password).verify_result()
            certs.append(cert5)
        if not is_bug_active(4222041):
            with allure.independent_step('Import cert6 using bundle URI + long pass (customer bug)'):
                bundle_uri = generate_scp_uri_using_player(scp_player, cert6.p12_bundle)
                security.certificate.cert_id[cert6.name].action_import(uri_bundle=bundle_uri,
                                                                       passphrase=cert6.p12_password).verify_result()
                certs.append(cert6)
    with allure.step('Show certs – expect all imported certs in output'):
        out = OutputParsingTool.parse_json_str_to_dictionary(security.certificate.show()).get_returned_value()
        assert all(cert.name in out for cert in
                   certs), f'not all expected certs names in show output\nexpected: {[cert.name for cert in certs]}\nout:\n{out}'
    with allure.step('Verify files in expected locations'):
        for cert in certs:
            with allure.independent_step(f'verify "{cert.name}" in expected cert locations'):
                verify_cert_in_expected_locations(cert.name, engines.dut)
    rand_cert: CertInfo = random.choice(certs)
    with allure.step(f'Show a single cert "{rand_cert.name}" – expect fields {CertShowFields.ALL_FIELDS}'):
        out_single = OutputParsingTool.parse_json_str_to_dictionary(
            security.certificate.cert_id[rand_cert.name].show()).get_returned_value()
        assert all(field in out_single for field in
                   CertShowFields.ALL_FIELDS), f'not all expected fields in single cert show output\nexpected: {CertShowFields.ALL_FIELDS}\nout:\n{out}'
    with allure.step('verify show values'):
        for field in CertShowFields.ALL_FIELDS:
            if field == CertShowFields.INSTALLED:
                with allure.independent_step('Verify installed empty {}'):
                    assert out_single[
                        field] == {}, f'field {field} not as expected\nexpected: {"{}"}\nactual: {out_single[field]}'
            else:
                with allure.independent_step(f'verify {field} not empty'):
                    assert out_single[field] != '', f'field {field} not as expected\nexpected: not empty\nactual: ""'
    with allure.step('verify inner show commands on certificate'):
        expected_str_in_content = f'DNS:{rand_cert.dn}'
        with allure.independent_step(f'Show dump of any cert – expect "{expected_str_in_content}" in output (sanity)'):
            dump_out_str = security.certificate.cert_id[rand_cert.name].dump.show()
            assert expected_str_in_content in dump_out_str, f'"{expected_str_in_content}" was not found in show dump output of {rand_cert.name}\nout:\n{dump_out_str}'
        with allure.independent_step('Show installed of any cert – expect empty {}'):
            installed_out = OutputParsingTool.parse_json_str_to_dictionary(
                security.certificate.cert_id[rand_cert.name].installed.show()).get_returned_value()
            assert installed_out == {}, f'installed output not as expected\nexpected: {"{}"}\nactual: {installed_out}'
    with allure.step(f'Delete a cert: {rand_cert.name}'):
        security.certificate.cert_id[rand_cert.name].action_delete().verify_result()
    with allure.step('Show all certs – expect deleted cert not exist'):
        out = OutputParsingTool.parse_json_str_to_dictionary(security.certificate.show()).get_returned_value()
        assert rand_cert.name not in out, f'deleted cert {rand_cert.name} unexpectedly exists in show output'
    with allure.step(f'Verify deleted cert {rand_cert.name} not exists in expected locations'):
        verify_cert_in_expected_locations(rand_cert.name, engines.dut, False)


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cert_mgmt_import_cert_data_bad_param(test_api, engines, scp_player, clear_certs):
    """
    Verify that import cert with bad params rejected

    1. Empty
    2. random short string
    3. real cert that injected letter in the middle
    """
    TestToolkit.tested_api = test_api

    security = System().security

    cert = TestCert.cert_valid_1.copy()
    real_data = cert.get_cert_content_str()
    index = real_data.find('-----END PRIVATE KEY-----')
    certs_datas = {'cert-empty-string': '', 'cert-rand-string': randomize_hex_str(10)}  # , 'cert-messed-data': real_data[:index - 5] + 'ALON' + real_data[index - 1:]}

    with allure.step('import certs using bad data params - expect fail and not in output'):
        with allure.independent_step('try import cert with using data param with bad values'):
            for cert_name, data in certs_datas.items():
                with allure.independent_step(cert_name):
                    security.certificate.cert_id[cert_name].action_import(data=data).verify_result(False)
        with allure.independent_step('verify no cert in show'):
            out = OutputParsingTool.parse_json_str_to_dictionary(security.certificate.show()).get_returned_value()
            assert out == {}, f'output not as expected\nexpected: {"{}"}\nactual: {out}'
        with allure.independent_step('Verify certs not exist in expected locations'):
            for cert_name in certs_datas.keys():
                with allure.independent_step(cert_name):
                    verify_cert_in_expected_locations(cert_name, engines.dut, False)


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cert_mgmt_import_cert_uri_bad_param(test_api, engines, scp_player, clear_certs):
    """
    Verify that import cert with bad params rejected

    1. empty values
    2. specify only private/public
    3. random string as url
    """
    TestToolkit.tested_api = test_api

    security = System().security

    cert = TestCert.cert_valid_1.copy()

    private_uri = generate_scp_uri_using_player(scp_player, cert.private)
    public_uri = generate_scp_uri_using_player(scp_player, cert.public)

    private, public = 'private', 'public'
    bad_certs = {
        # empty values
        'cert1': {private: private_uri, public: ''},
        'cert2': {private: '', public: public_uri},
        'cert3': {private: '', public: ''},
        # missing param
        'cert4': {private: private_uri, public: None},
        'cert5': {private: None, public: public_uri},
        'cert6': {private: None, public: None},
        # rand url
        'cert7': {private: private_uri, public: 'xyz'},
        'cert8': {private: 'xyz', public: public_uri},
        'cert9': {private: 'xyz', public: 'xyz'},
    }

    with allure.step('import certs using bad data params - expect fail and not in output'):
        with allure.independent_step('try import cert with using data param with bad values'):
            for cert_name, params in bad_certs.items():
                with allure.independent_step(cert_name):
                    security.certificate.cert_id[cert_name].action_import(uri_private_key=params[private],
                                                                          uri_public_key=params[public]).verify_result(
                        False)
        with allure.independent_step('verify no cert in show'):
            out = OutputParsingTool.parse_json_str_to_dictionary(security.certificate.show()).get_returned_value()
            assert out == {}, f'output not as expected\nexpected: {"{}"}\nactual: {out}'
        with allure.independent_step('Verify certs not exist in expected locations'):
            for cert_name in bad_certs.keys():
                with allure.independent_step(cert_name):
                    verify_cert_in_expected_locations(cert_name, engines.dut, False)


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cert_mgmt_import_cert_bundle_bad_param(test_api, engines, scp_player, clear_certs):
    """
    Verify that import cert with bad params rejected

    1. empty value
    2. random string as url
    3. use wrong pass to bundle with pass
    4. don't give pass to bundle with pass
    5. use empty pass to bundle with pass
    6. use pass to bundle without pass
    """
    TestToolkit.tested_api = test_api

    security = System().security

    cert_with_pass = TestCert.cert_valid_1.copy()
    cert_with_no_pass = TestCert.cert_valid_1_no_passphrase.copy()

    bundle_with_pass_uri = generate_scp_uri_using_player(scp_player, cert_with_pass.p12_bundle)
    bundle_with_no_pass_uri = generate_scp_uri_using_player(scp_player, cert_with_no_pass.p12_bundle)
    rand_str = 'xyz'

    description, uri, passphrase = 'description', 'uri', 'passphrase'
    bad_certs = {
        'cert1': {description: 'empty uri value', uri: "", passphrase: cert_with_pass.p12_password},
        'cert2': {description: 'random string as url', uri: rand_str, passphrase: cert_with_pass.p12_password},
        'cert3': {description: 'wrong pass to bundle with pass', uri: bundle_with_pass_uri, passphrase: rand_str},
        'cert4': {description: "don't give pass to bundle with pass", uri: bundle_with_pass_uri, passphrase: None},
        'cert5': {description: 'empty pass to bundle with pass', uri: bundle_with_pass_uri, passphrase: ""},
        'cert6': {description: 'pass to bundle without pass', uri: bundle_with_no_pass_uri,
                  passphrase: cert_with_pass.p12_password},
    }

    with allure.step('import certs using bad data params - expect fail and not in output'):
        with allure.independent_step('try import cert with using data param with bad values'):
            for cert_name, info in bad_certs.items():
                with allure.independent_step(f'{cert_name} - {info[description]}'):
                    security.certificate.cert_id[cert_name].action_import(uri_bundle=info[uri],
                                                                          passphrase=info[passphrase]).verify_result(
                        False)
        with allure.independent_step('verify no cert in show'):
            out = OutputParsingTool.parse_json_str_to_dictionary(security.certificate.show()).get_returned_value()
            assert out == {}, f'output not as expected\nexpected: {"{}"}\nactual: {out}'
        with allure.independent_step('Verify certs not exist in expected locations'):
            for cert_name, info in bad_certs.items():
                with allure.independent_step(f'{cert_name} - {info[description]}'):
                    verify_cert_in_expected_locations(cert_name, engines.dut, False)


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cert_mgmt_delete_cert_bad_param(test_api, engines, scp_player, clear_certs):
    """
    Verify that delete cert with bad params rejected and doesn't affect other imported certs
    """
    TestToolkit.tested_api = test_api

    security = System().security

    certs: List[CertInfo] = [TestCert.cert_valid_1.copy(f'cert{i + 1}') for i in range(5)]

    with allure.step('import some certs'):
        import_certificates(scp_player, engines.dut, certs)
    with allure.step('delete cert that does not exist'):
        security.certificate.cert_id['xyz'].action_delete().verify_result(False)
    with allure.step('verify all imported certs still exist'):
        out = OutputParsingTool.parse_json_str_to_dictionary(security.certificate.show()).get_returned_value()
        for cert in certs:
            with allure.independent_step(cert.name):
                with allure.independent_step('verify exists in show'):
                    assert cert.name in out, f'{cert.name} does not exist in show certificate output, but expected to exist\n{out}'
                with allure.independent_step('verify exists in expected locations'):
                    verify_cert_in_expected_locations(cert.name, engines.dut)


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cert_mgmt_import_cert_unique_id(test_api, engines, scp_player, clear_certs):
    """
    Verify that must use unique cert id

    1. import cert1
    2. import another cert with same id – expect rejected
    """
    TestToolkit.tested_api = test_api

    security = System().security
    cert = TestCert.cert_valid_1
    bundle_uri = generate_scp_uri_using_player(scp_player, cert.p12_bundle)

    title, cert_name, expect = 'title', 'cert_name', 'expect'
    cases: List[dict] = [
        {title: 'import cert1', cert_name: 'cert1', expect: True},
        {title: 'import cert2', cert_name: 'cert2', expect: True},
        {title: 'repeat cert1', cert_name: 'cert1', expect: False},
        {title: 'import cert3', cert_name: 'cert3', expect: True},
        {title: 'repeat cert2', cert_name: 'cert2', expect: False},
        {title: 'import cert4', cert_name: 'cert4', expect: True},
    ]

    with allure.step('test importing in several cases'):
        for case in cases:
            with allure.independent_step(f'{case[title]} - expect: {case[expect]}'):
                security.certificate.cert_id[case[cert_name]].action_import(uri_bundle=bundle_uri,
                                                                            passphrase=cert.p12_password).verify_result(
                    case[expect])
    with allure.step('show certificates'):
        out = OutputParsingTool.parse_json_str_to_dictionary(security.certificate.show()).get_returned_value()
    with allure.step('verify existence of all certs'):
        certs = ['cert1', 'cert2', 'cert3', 'cert4']
        for cert in certs:
            with allure.independent_step(f'{case[cert_name]} - expect: {case[expect]}'):
                with allure.independent_step('verify in show'):
                    assert cert in out, f'cert {cert} does not appear in certificate show output but expected to exist\n{out}'
                with allure.independent_step('verify files'):
                    verify_cert_in_expected_locations(cert, engines.dut)


""" functional tests """


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
def test_cert_mgmt_use_cert_for_rest_api_tls(test_api, test_flow, engines, scp_player, clear_certs):
    """
    Verify that valid imported cert can be used for REST server TLS

    1. import cert
    2. bind cert to rest server (system api certificate)
    3. send unsecured client request – success
    4. send client request using non/proper CA - fail/success
    """
    TestToolkit.tested_api = test_api
    is_good_flow = test_flow == TestFlowType.GOOD_FLOW
    system = System()
    security = system.security
    cert = TestCert.cert_valid_1.copy('cert1')
    bundle_uri = generate_scp_uri_using_player(scp_player, cert.p12_bundle)

    with allure.step('import cert'):
        security.certificate.cert_id[cert.name].action_import(uri_bundle=bundle_uri,
                                                              passphrase=cert.p12_password).verify_result()
    with allure.step('bind cert to rest server (system api certificate)'):
        system.api.set(CERTIFICATE, cert.name, apply=True).verify_result()
    if is_good_flow:
        with allure.step('send unsecured client request – expect success'):
            send_curl_with_and_verify(cert.dn, engines.dut.username, engines.dut.password, EncryptionMode.DISABLED)
    with allure.step(
            f'send client request using {"" if is_good_flow else "non-"}proper CA – expect {"success" if is_good_flow else "fail"}'):
        client_ca = cert if is_good_flow else TestCert.cert_valid_2
        send_curl_with_and_verify(cert.dn, engines.dut.username, engines.dut.password, EncryptionMode.TLS, client_ca,
                                  None, is_good_flow)


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.security
def test_cert_mgmt_import_cert_from_file(engines, random_api, dut_hostname):
    """
    Verify that cert can be imported from file
    1. Copy cert to dut
    2. Import cert from file no localhost
    3. Import cert from file with localhost
    4. Verify cert is imported
    """
    TestToolkit.tested_api = random_api
    security = System().security
    test_dir = '/tmp/test_cert_mgmt_import_cert_from_file'
    cert_filename = 'sasha_cert'
    cert_pass = 'sasha_pass'
    cert_dir = f'{test_dir}/sasha_cert'
    ca_dir = f'{test_dir}/sasha_ca'
    ca_filename = 'sasha_cert'
    cert_id = 'sasha_cert'

    with allure.step('generate cert on dut'):
        cert_generator = CertificateGeneratorOnRemoteHost(engines.dut)
        cert_generator.generate_cert(
            cert_dir, cert_filename, 'sasha', engines.dut.ip, dut_hostname,
            ca_dir, ca_filename, cert_pass
        )
        bundle_uri = generate_file_location_uri(f'{cert_dir}/{cert_filename}.p12')
        cert_uri_localhost = generate_file_location_uri(f'{cert_dir}/{cert_filename}.pem', localhost=True)
        key_uri = generate_file_location_uri(f'{cert_dir}/{cert_filename}.key', localhost=True)
    cert_name = f"{cert_id}_no_localhost"
    cert_name_localhost = f"{cert_id}_localhost"

    with allure.step('test importing certs from file protocol'):
        with allure.independent_step(f'import cert from file no localhost'):
            security.certificate.cert_id[cert_name].action_import(uri_bundle=bundle_uri,
                                                                  passphrase=cert_pass).verify_result()
        with allure.independent_step(f'import cert from file with localhost'):
            security.certificate.cert_id[cert_name_localhost].action_import(uri_private_key=key_uri,
                                                                            uri_public_key=cert_uri_localhost).verify_result()
    with allure.step('show certificates'):
        out = OutputParsingTool.parse_json_str_to_dictionary(security.certificate.show()).get_returned_value()
    with allure.step('verify existence of all certs'):
        with allure.independent_step('verify in show'):
            assert cert_name in out, f'cert {cert_name} does not appear in certificate show output but expected to exist\n{out}'
            assert cert_name_localhost in out, f'cert {cert_name_localhost} does not appear in certificate show output but expected to exist\n{out}'
        with allure.independent_step('verify files'):
            verify_cert_in_expected_locations(cert_name, engines.dut)
            verify_cert_in_expected_locations(cert_name_localhost, engines.dut)


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.security
def test_local_cert_generated_after_timezone_change(engines, dut_hostname):
    """
    Case of customer bug that tried to configure locally generated cert after timezone changed to PST

    https://redmine.mellanox.com/issues/4252131
    https://nvbugspro.nvidia.com/bug/5048616

    Steps:
    1. change dut timezone
    2. generate cert locally on dut
    3. import local cert
    4. set local cert to api certificate
    5. verify successfully configured in show
    """
    test_dir = '/tmp/test'
    cert_filename = 'cert'
    cert_pass = 'secret'
    cert_dir = f'{test_dir}/cert'
    ca_dir = f'{test_dir}/ca'
    ca_filename = 'cert'
    cert_id = 'local-cert'

    client_timezone = 'America/Los_Angeles'

    with allure.step('clear existing certs'):
        clear_existing_certs(engines)
    with allure.step('change timezone'):
        system = System()
        system.datetime.set('timezone', client_timezone, apply=True).verify_result()
    with allure.step('generate cert on dut'):
        cert_generator = CertificateGeneratorOnRemoteHost(engines.dut)
        cert_generator.generate_cert(
            cert_dir, cert_filename, 'alon', engines.dut.ip, dut_hostname,
            ca_dir, ca_filename, cert_pass
        )
    with allure.step('import cert to system'):
        system.security.certificate.cert_id[cert_id].action_import(
            passphrase=cert_pass,
            uri_bundle=generate_scp_uri_using_player(
                LinuxSshEngine('localhost', engines.dut.username, engines.dut.password),
                f'{cert_dir}/{cert_filename}.p12'
            )
        ).verify_result()
    with allure.step('set to api'):
        system.api.set(CERTIFICATE, cert_id, apply=True).verify_result()
    with allure.step('verify successfully changed in show'):
        out = OutputParsingTool.parse_json_str_to_dictionary(system.api.show()).get_returned_value()
        assert out[CERTIFICATE] == cert_id, f'unexpected cert in api show\nexpected: {cert_id}\nactual: {out[CERTIFICATE]}'


@pytest.mark.system
@pytest.mark.certificate
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cert_mgmt_import_raw_chain(test_api, engines, clear_certs):
    """
    Verify that importing a certificate chain raw with system security import data works

    1. Import a certificate chain using data import
    2. Verify the import was successful
    3. Use openssl to verify the chain exists and matches expected pattern
    """
    TestToolkit.tested_api = test_api
    security = System().security

    cert_name = "imported-raw-chain"
    cert_info = TestCert.cert_chain_raw_1  # Use the constant defined in constants.py

    with allure.step('Get certificate chain data'):
        try:
            chain_data = cert_info.get_cert_content_str()
            if not chain_data:
                pytest.fail(f"Could not read certificate chain content from {cert_info.public}")
        except Exception as e:
            pytest.fail(f"Error reading certificate chain file {cert_info.public}: {e}")

    with allure.step('Import certificate chain using data'):
        security.certificate.cert_id[cert_name].action_import(data=chain_data).verify_result()

    with allure.step('Verify certificate chain import success'):
        verify_cert_in_expected_locations(cert_name, engines.dut)
        security_cert_output_dict: dict = OutputParsingTool.parse_json_str_to_dictionary(security.certificate.show()).get_returned_value()
        assert cert_name in security_cert_output_dict, f"Certificate chain {cert_name} not found in show output:\n{security_cert_output_dict}"

    with allure.step('Verify certificate chain with openssl'):
        cmd_runner = CmdRunner()
        with tempfile.NamedTemporaryFile() as tmp_cert_file:
            engines.dut.copy_file(source_file=f"{DUT_IMPORTED_CERTS_PUBLIC_DIR}/{cert_name}.crt", dest_file=tmp_cert_file.name,
                                  file_system=os.path.dirname(tmp_cert_file.name), direction='get')
            verify_cmd = OpenSslCmdBuilder().subcommand("verify").CAfile(f"{TEST_CERTS}/cert-chain/rootca.crt").option("untrusted", f"{TEST_CERTS}/cert-chain/interca.crt").positional_arg(tmp_cert_file.name).get_command_string()
            openssl_verify_result = cmd_runner.run_cmd(verify_cmd)
            assert "OK" in openssl_verify_result, f"Certificate chain verification failed: {openssl_verify_result}"
