import os
import random
from typing import List, Dict, Optional

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import ApiType, UserRole, RebootTestFlowType
from ngts.nvos_tools.infra.NvCommand import NvCommand
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.conftest import get_dut_hostname
from ngts.tests_nvos.general.security.api_server.mtls.spiffe_id.conftest import cleanup_spiffe
from ngts.tests_nvos.general.security.api_server.mtls.spiffe_id.constants import INVALID_SPIFFE_ERR, \
    SPIFFE_UNIQUENESS_ERR, SecurityMode, INCOMPLETE_ERR_PER_API
from ngts.tests_nvos.general.security.api_server.mtls.spiffe_id.helpers import generate_rand_spiffe_id, \
    setup_api_security_mode, get_tmp_revision_number_for_test_only
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.system.gnmi.conftest import scp_player
from ngts.tests_nvos.general.security.helpers import import_certs_safely, get_test_certs_dir_location, \
    set_new_random_users, import_cas_safely, generate_certs, cleanup_aaa_users
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tests_nvos.helpers.general_helpers import generate_rand_str, verify_result_obj_failure, run_cmd
from ngts.tests_nvos.system.gnmi.helpers import get_scp_player
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.general.security.mtls.generic_testing.helpers import VerifyBuilderFunc
from ngts.tests_nvos.general.security.test_api_server_security.helpers import build_curl_cmd_and_verify, build_curl_set_cmd_and_verify


@pytest.mark.security
@pytest.mark.mtls
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_api_spiffe_cli(test_api, local_admin_users: List[UserInfo], engines):
    """
    Verify that all CLI work and check values change properly in show

    Steps:
    1. Run show commands
    2. Verify outputs contain the required fields
    3. Run set command
    4. Verify in show commands
    5. Unset
    6. Verify in show commands
    """
    TestToolkit.update_apis(test_api)
    system = System()
    rand_user: UserInfo = random.choice(local_admin_users)
    user_obj = system.aaa.user.user_id[rand_user.username]
    spifs = [generate_rand_spiffe_id() for _ in range(3)]
    spif1 = spifs[0]

    with allure.step('Run show commands and verify outputs contain the required fields'):
        out = OutputParsingTool.parse_json_str_to_dictionary(user_obj.spiffe_id.show()).get_returned_value()
        assert out == {}, f'output of general spiffe resource is not empty as expected. actual: {out}'
        out = OutputParsingTool.parse_json_str_to_dictionary(user_obj.spiffe_id.spiffe[spif1].show()).get_returned_value()
        assert out == {}, f'output of specific spiffe resource is not empty as expected. actual: {out}'
    with allure.step('Set multiple spiffes to single user'):
        for spif in spifs:
            with allure.independent_step(spif):
                user_obj.spiffe_id.spiffe[spif].set().verify_result()
    with allure.step('apply'):
        user_obj._general_cli_wrapper.apply_config(engines.dut, verify_execution=True)
    with allure.step('Verify in show commands'):
        with allure.independent_step('general spiffe-id show of a user'):
            out = OutputParsingTool.parse_json_str_to_dictionary(user_obj.spiffe_id.show()).get_returned_value()
            expected = {spif: {} for spif in spifs}
            assert out == expected, f'output of general spiffe resource is not as expected:\nexpected: {expected}\nactual: {out}'
        with allure.step('check show of specific spiffe'):
            for spif in spifs:
                with allure.independent_step(spif):
                    out = OutputParsingTool.parse_json_str_to_dictionary(
                        user_obj.spiffe_id.spiffe[spif].show()).get_returned_value()
                    assert out == {}, f'output of specific spiffe is not empty as expected.\nactual: {out}'
    with allure.step('Unset and verify in show'):
        with allure.independent_step('unset specific spif'):
            with allure.step('unset'):
                user_obj.spiffe_id.spiffe[spif1].unset(apply=True).verify_result()
            with allure.step('verify deleted in show'):
                out = OutputParsingTool.parse_json_str_to_dictionary(user_obj.spiffe_id.show()).get_returned_value()
                assert out != {}, 'show output is unexpectedly empty after unsetting a single spiffe'
                assert spif1 not in out, f'spiffe "{spif1}" unexpectedly exist in show output after unset'
        with allure.independent_step('unset all spifs'):
            with allure.step('unset'):
                user_obj.spiffe_id.unset(apply=True).verify_result()
            with allure.step('verify deleted in show'):
                out = OutputParsingTool.parse_json_str_to_dictionary(user_obj.spiffe_id.show()).get_returned_value()
                assert out == {}, 'show output is not empty after unsetting all spiffes'


@pytest.mark.security
@pytest.mark.mtls
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_api_spiffe_valid_value_special_cases(test_api, local_adminuser: UserInfo, engines):
    """
    Verify that set with valid spiffe works
    """
    TestToolkit.update_apis(test_api)
    user_obj = System().aaa.user.user_id[local_adminuser.username]

    class Case:
        def __init__(self, name: str, spif_val: str):
            self.name: str = name
            self.spif_val: str = spif_val

    cases: List[Case] = [
        Case('long spif', generate_rand_spiffe_id(500, 500)),
        Case('short spif', generate_rand_spiffe_id(1, 1)),
    ]

    with allure.step('Set invalid spiffes and verify err'):
        for case in cases:
            with allure.independent_step(f'{case.name} : "{case.spif_val}"'):
                user_obj.spiffe_id.spiffe[case.spif_val].set().verify_result()
        with allure.step('apply'):
            user_obj._general_cli_wrapper.apply_config(engines.dut, verify_execution=True)
    with allure.step('Verify in show – expect the value exists'):
        out = OutputParsingTool.parse_json_str_to_dictionary(user_obj.spiffe_id.show()).get_returned_value()
        missing_valid_spiffs = [case.spif_val for case in cases if case.spif_val not in out]
        assert not missing_valid_spiffs, f'missing values "{missing_valid_spiffs}" in show spiffe output: {out}'


@pytest.mark.security
@pytest.mark.mtls
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_api_spiffe_invalid_value(test_api, local_adminuser: UserInfo):
    """
    Verify that set with bad param rejected
    Steps:
    1. Set invalid spiffe (empty, bad formatted string, too long?, too short?)
    2. Verify set command rejected
    3. Verify in show – expect the value doesn't exist
    """
    TestToolkit.update_apis(test_api)
    user_obj = System().aaa.user.user_id[local_adminuser.username]

    class Case:
        def __init__(self, name: str, spif_val: str, expected_err: str):
            self.name: str = name
            self.spif_val: str = spif_val
            self.expected_err: str = expected_err

    cases: List[Case] = [
        Case('empty spif ""', '', INCOMPLETE_ERR_PER_API[test_api]),
        Case('rand str (not well spiffe formatted)', generate_rand_str(10), INVALID_SPIFFE_ERR),
    ]

    with allure.step('Set invalid spiffes and verify err'):
        for case in cases:
            with allure.independent_step(f'{case.name} : "{case.spif_val}"'):
                res: ResultObj = user_obj.spiffe_id.spiffe[case.spif_val].set()
                verify_result_obj_failure(res, case.expected_err)
    with allure.step("Verify in show - expect the value doesn't exist"):
        out = OutputParsingTool.parse_json_str_to_dictionary(user_obj.spiffe_id.show()).get_returned_value()
        existing_invalid_spiffs = [case.spif_val for case in cases if case.spif_val in out]
        assert not existing_invalid_spiffs, f'invalid values "{existing_invalid_spiffs}" exist in show spiffe output: {out}'


@pytest.mark.security
@pytest.mark.mtls
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_api_spiffe_uniqueness_apply_together(test_api, local_admin_users: List[UserInfo]):
    """
    Verify that can't set same SPIFFE to multiple users

    Steps:
    1. Set spiffe to user1
    2. Try to set same spiffe to user2 - expect failure
    3. Verify only user1 has the spiffe
    """
    TestToolkit.update_apis(test_api)
    system = System()
    user1, user2 = local_admin_users[0], local_admin_users[1]
    user1_obj, user2_obj = system.aaa.user.user_id[user1.username], system.aaa.user.user_id[user2.username]
    spif = generate_rand_spiffe_id()

    with allure.step('test applying same spiffe to 2 users fail (apply on both users together)'):
        with allure.step(f'set spiffe "{spif}" to both users'):
            user1_obj.spiffe_id.spiffe[spif].set().verify_result()
            res: ResultObj = user2_obj.spiffe_id.spiffe[spif].set(apply=True)
        with allure.independent_step('verify failure'):
            verify_result_obj_failure(res, SPIFFE_UNIQUENESS_ERR.format(spif))
        with allure.independent_step("verify both users don't have the spiffe"):
            for user in [user1, user2]:
                with allure.independent_step(user.username):
                    user_obj = system.aaa.user.user_id[user.username]
                    out = OutputParsingTool.parse_json_str_to_dictionary(user_obj.spiffe_id.show()).get_returned_value()
                    assert spif not in out, f'spif "{spif}" unexpectedly found in user ({user.username}) spiffes\n{out}'


@pytest.mark.security
@pytest.mark.mtls
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_api_spiffe_uniqueness_apply_separately(test_api, local_admin_users: List[UserInfo]):
    """
    Verify that can't set same SPIFFE to multiple users

    Steps:
    1. Set spiffe1 to user1 + apply
    2. Set spiffe1 to user2 + apply
    3. Verify 2nd apply failed
    4. verify user1 has spiffe1, but user2 doesn't have spiffe1
    """
    TestToolkit.update_apis(test_api)
    system = System()
    user1, user2 = local_admin_users[0], local_admin_users[1]
    user1_obj, user2_obj = system.aaa.user.user_id[user1.username], system.aaa.user.user_id[user2.username]
    spif = generate_rand_spiffe_id()

    with allure.step('test applying same spiffe to 2 users fail (apply separately)'):
        with allure.step(f'set spiffe "{spif}" to both users'):
            user1_obj.spiffe_id.spiffe[spif].set(apply=True).verify_result()
            res: ResultObj = user2_obj.spiffe_id.spiffe[spif].set(apply=True)
        with allure.independent_step('verify failure for 2nd user apply'):
            verify_result_obj_failure(res, SPIFFE_UNIQUENESS_ERR.format(spif))
        with allure.independent_step(f"verify only user1 ({user1.username}) has spiffe"):
            with allure.independent_step(user1.username):
                out = OutputParsingTool.parse_json_str_to_dictionary(user1_obj.spiffe_id.show()).get_returned_value()
                assert spif in out, f'spif "{spif}" unexpectedly missing in user1 ({user1.username}) spiffes\n{out}'
            with allure.independent_step(user2.username):
                out = OutputParsingTool.parse_json_str_to_dictionary(user2_obj.spiffe_id.show()).get_returned_value()
                assert spif not in out, f'spif "{spif}" unexpectedly found in user2 ({user2.username}) spiffes\n{out}'


Cert = Optional[CertInfo]
User = Optional[UserInfo]


class Case:
    def __init__(self, name: str, creds: User, cert: Cert, expect_authorized_user: User):
        self.name: str = name
        self.creds: User = creds
        self.cert: Cert = cert
        self.expect_authorized_user: User = expect_authorized_user

    def verify_show(self, host: str, ca: Cert, insecured: bool, verify_builder_func: VerifyBuilderFunc):
        expect_success = self.expect_authorized_user is not None
        with allure.step(f'check show: {expect_success}'):
            verify_builder_func(host, self.creds, expect_success, ca, insecured, self.cert, None)

    def verify_set(self, revision, host: str, ca: Cert, insecured: bool, verify_builder_func: VerifyBuilderFunc):
        expect_success = self.expect_authorized_user is not None and self.expect_authorized_user.role == UserRole.ADMIN
        with allure.step(f'check set: {expect_success}'):
            verify_builder_func(host, self.creds, expect_success, ca, insecured, self.cert, revision)


class TestSetup:
    def __init__(self, user1: UserInfo, user2: UserInfo, user3: UserInfo, cert_no_spif: CertInfo, cert_2_spifs: CertInfo,
                 cert_spif_not_exists: CertInfo, cert_spif_of_user1_1: CertInfo, cert_spif_of_user1_2: CertInfo,
                 cert_spif_of_user2: CertInfo, server_cert: CertInfo, server_ca: CertInfo, spiffes_info: Dict[str, List[str]]):
        self.user1: UserInfo = user1
        self.user2: UserInfo = user2
        self.user3: UserInfo = user3
        self.cert_no_spif: CertInfo = cert_no_spif
        self.cert_2_spifs: CertInfo = cert_2_spifs
        self.cert_spif_not_exists: CertInfo = cert_spif_not_exists
        self.cert_spif_of_user1_1: CertInfo = cert_spif_of_user1_1
        self.cert_spif_of_user1_2: CertInfo = cert_spif_of_user1_2
        self.cert_spif_of_user2: CertInfo = cert_spif_of_user2
        self.server_cert: CertInfo = server_cert
        self.server_ca: CertInfo = server_ca
        self.spiffes_info: Dict[str, List[str]] = spiffes_info


def setup_test(dut_hostname, engines, scp_player, save_users: bool = False, cert_name_prefix: str = 'api') -> TestSetup:
    dut_engine = engines.dut
    system = System()
    dn = dut_hostname
    ip = dut_engine.ip
    certs_location = get_test_certs_dir_location('spiffe', dut_hostname)

    with allure.step('randomize spiffes'):
        spifs = [generate_rand_spiffe_id() for _ in range(4)]

    with allure.step('prepare client certs'):
        cn = 'nvos-client'
        cert_no_spif = CertInfo(f'{cert_name_prefix}-cert', 'without spiffe', '', '', '', '', dn, ip, '', f'{cn}')
        cert_2_spifs = CertInfo(f'{cert_name_prefix}-2-spiffs', '2 spiffs', '', '', '', '', dn, ip, '', f'{cn}-9', [spifs[0], spifs[1]])
        cert_spif_not_exists = CertInfo(f'{cert_name_prefix}-cert0', 'spiffe that no user has', '', '', '', '', dn, ip, '', f'{cn}-0', [spifs[0]])
        cert_spif_of_user1_1 = CertInfo(f'{cert_name_prefix}-cert11', 'spiffe of user1 #1', '', '', '', '', dn, ip, '', f'{cn}-11', [spifs[1]])
        cert_spif_of_user1_2 = CertInfo(f'{cert_name_prefix}-cert12', 'spiffe of user1 #2', '', '', '', '', dn, ip, '', f'{cn}-12', [spifs[2]])
        cert_spif_of_user2 = CertInfo(f'{cert_name_prefix}-cert2', 'spiffe of user2', '', '', '', '', dn, ip, '', f'{cn}-2', [spifs[3]])
        clients_certs = [cert_no_spif, cert_2_spifs, cert_spif_not_exists, cert_spif_of_user1_1, cert_spif_of_user1_2,
                         cert_spif_of_user2]
        client_certs_dir = os.path.join(certs_location, 'client_certs')
        generate_certs(client_certs_dir, clients_certs)

    with allure.step('prepare server cert'):
        server_certs_dir = os.path.join(certs_location, 'server_certs')
        server_certs = [
            CertInfo(f'{cert_name_prefix}-server-cert', 'server cert', '', '', '', '', dn, ip, '', f'{dn}'),
        ]
        server_cert: CertInfo = server_certs[0]
        generate_certs(server_certs_dir, server_certs)

    with allure.step('set local users'):
        # Stage the admin user and apply once together with the monitors: a single apply, and the
        # pending revision never holds incomplete AAA users that poison a later apply (e.g. TACACS
        # auth-order) with "User must have a password".
        admins = set_new_random_users(1, UserRole.ADMIN, apply=False, engine=dut_engine)
        try:
            monitors = set_new_random_users(2, UserRole.MONITOR, apply=True, engine=dut_engine)
        except Exception:
            cleanup_aaa_users(admins, engine=dut_engine)  # else the staged admin poisons the next apply
            raise
        user1: UserInfo = admins[0]
        user2: UserInfo = monitors[0]
        user3: UserInfo = monitors[1]

    if save_users:
        with allure.step('save config'):
            NvueGeneralCli.save_config(engines.dut)

    with allure.step('bind spiffes to users'):
        system.aaa.user.user_id[user1.username].spiffe_id.spiffe[spifs[1]].set().verify_result()
        system.aaa.user.user_id[user1.username].spiffe_id.spiffe[spifs[2]].set().verify_result()
        system.aaa.user.user_id[user2.username].spiffe_id.spiffe[spifs[3]].set().verify_result()

    with allure.step('attach spiffes info in setup object'):
        spiffes_info: Dict[str, List[str]] = {
            user1.username: [spifs[1], spifs[2]],
            user2.username: [spifs[3]],
        }

    with allure.step('apply all prepared configuration'):
        system._general_cli_wrapper.apply_config(dut_engine, verify_execution=True)

    with allure.step('import server cert & CA'):
        import_certs_safely(server_certs, scp_player)
        import_cas_safely([clients_certs[0]], scp_player)

    return TestSetup(user1, user2, user3, cert_no_spif, cert_2_spifs, cert_spif_not_exists, cert_spif_of_user1_1,
                     cert_spif_of_user1_2, cert_spif_of_user2, server_cert, clients_certs[0], spiffes_info)


def spiffe_setup_users(setup: Optional[TestSetup]) -> Optional[List[UserInfo]]:
    if setup is None:
        return None
    return [setup.user1, setup.user2, setup.user3]


@pytest.mark.security
@pytest.mark.mtls
def test_api_spiffe_core_functionality(dut_hostname, engines, scp_player):
    """
    preparation for test:
    - 3 local users (usr1, usr2, usr3)
    - one CA that issues all client certs (for server)
    - client certs:
        - cert without spiffe
        - cert0 with spiffe that no user will have
        - cert11 with spiffe11 that usr1 will have
        - cert12 with spiffe12 that usr1 will have
        - cert2 with spiffe2 that usr2 will have
    - prepare api configuration as the required security mode
        - unsecured: no further configuration
        - tls: import server cert and bind to api
        - mtls: as tls + import client CA and bind to api mtls ca-certificate
    """
    setup: TestSetup = setup_test(dut_hostname, engines, scp_player)

    bad_pw = 'bad-password'
    user1_bad_pw = UserInfo(setup.user1.username, bad_pw, setup.user1.role)
    user2_bad_pw = UserInfo(setup.user2.username, bad_pw, setup.user2.role)
    user3_bad_pw = UserInfo(setup.user3.username, bad_pw, setup.user3.role)

    no_mtls_cases: List[Case] = [
        # no creds
        Case('no creds + no cert', None, None, None),
        Case(f'no creds + {setup.cert_2_spifs.info}', None, setup.cert_2_spifs, None),
        Case(f'no creds + {setup.cert_no_spif.info}', None, setup.cert_no_spif, None),
        Case(f'no creds + {setup.cert_spif_not_exists.info}', None, setup.cert_spif_not_exists, None),
        Case(f'no creds + {setup.cert_spif_of_user1_1.info}', None, setup.cert_spif_of_user1_1, None),
        Case(f'no creds + {setup.cert_spif_of_user1_2.info}', None, setup.cert_spif_of_user1_2, None),
        Case(f'no creds + {setup.cert_spif_of_user2.info}', None, setup.cert_spif_of_user2, None),
        # usr1 good pw
        Case(f'usr1 good creds + no cert', setup.user1, None, setup.user1),
        Case(f'usr1 good creds + {setup.cert_2_spifs.info}', setup.user1, setup.cert_2_spifs, setup.user1),
        Case(f'usr1 good creds + {setup.cert_no_spif.info}', setup.user1, setup.cert_no_spif, setup.user1),
        Case(f'usr1 good creds + {setup.cert_spif_not_exists.info}', setup.user1, setup.cert_spif_not_exists, setup.user1),
        Case(f'usr1 good creds + {setup.cert_spif_of_user1_1.info}', setup.user1, setup.cert_spif_of_user1_1, setup.user1),
        Case(f'usr1 good creds + {setup.cert_spif_of_user1_2.info}', setup.user1, setup.cert_spif_of_user1_2, setup.user1),
        Case(f'usr1 good creds + {setup.cert_spif_of_user2.info}', setup.user1, setup.cert_spif_of_user2, setup.user1),
        # usr1 bad pw
        Case(f'usr1 bad creds + no cert', user1_bad_pw, None, None),
        Case(f'usr1 bad creds + {setup.cert_2_spifs.info}', user1_bad_pw, setup.cert_2_spifs, None),
        Case(f'usr1 bad creds + {setup.cert_no_spif.info}', user1_bad_pw, setup.cert_no_spif, None),
        Case(f'usr1 bad creds + {setup.cert_spif_not_exists.info}', user1_bad_pw, setup.cert_spif_not_exists, None),
        Case(f'usr1 bad creds + {setup.cert_spif_of_user1_1.info}', user1_bad_pw, setup.cert_spif_of_user1_1, None),
        Case(f'usr1 bad creds + {setup.cert_spif_of_user1_2.info}', user1_bad_pw, setup.cert_spif_of_user1_2, None),
        Case(f'usr1 bad creds + {setup.cert_spif_of_user2.info}', user1_bad_pw, setup.cert_spif_of_user2, None),
        # usr2 good pw
        Case(f'usr2 good creds + no cert', setup.user2, None, setup.user2),
        Case(f'usr2 good creds + {setup.cert_2_spifs.info}', setup.user2, setup.cert_2_spifs, setup.user2),
        Case(f'usr2 good creds + {setup.cert_no_spif.info}', setup.user2, setup.cert_no_spif, setup.user2),
        Case(f'usr2 good creds + {setup.cert_spif_not_exists.info}', setup.user2, setup.cert_spif_not_exists, setup.user2),
        Case(f'usr2 good creds + {setup.cert_spif_of_user1_1.info}', setup.user2, setup.cert_spif_of_user1_1, setup.user2),
        Case(f'usr2 good creds + {setup.cert_spif_of_user1_2.info}', setup.user2, setup.cert_spif_of_user1_2, setup.user2),
        Case(f'usr2 good creds + {setup.cert_spif_of_user2.info}', setup.user2, setup.cert_spif_of_user2, setup.user2),
        # usr2 bad pw
        Case(f'usr2 bad creds + no cert', user2_bad_pw, None, None),
        Case(f'usr2 bad creds + {setup.cert_2_spifs.info}', user2_bad_pw, setup.cert_2_spifs, None),
        Case(f'usr2 bad creds + {setup.cert_no_spif.info}', user2_bad_pw, setup.cert_no_spif, None),
        Case(f'usr2 bad creds + {setup.cert_spif_not_exists.info}', user2_bad_pw, setup.cert_spif_not_exists, None),
        Case(f'usr2 bad creds + {setup.cert_spif_of_user1_1.info}', user2_bad_pw, setup.cert_spif_of_user1_1, None),
        Case(f'usr2 bad creds + {setup.cert_spif_of_user1_2.info}', user2_bad_pw, setup.cert_spif_of_user1_2, None),
        Case(f'usr2 bad creds + {setup.cert_spif_of_user2.info}', user2_bad_pw, setup.cert_spif_of_user2, None),
        # usr3 good pw
        Case(f'usr3 good creds + no cert', setup.user3, None, setup.user3),
        Case(f'usr3 good creds + {setup.cert_2_spifs.info}', setup.user3, setup.cert_2_spifs, setup.user3),
        Case(f'usr3 good creds + {setup.cert_no_spif.info}', setup.user3, setup.cert_no_spif, setup.user3),
        Case(f'usr3 good creds + {setup.cert_spif_not_exists.info}', setup.user3, setup.cert_spif_not_exists, setup.user3),
        Case(f'usr3 good creds + {setup.cert_spif_of_user1_1.info}', setup.user3, setup.cert_spif_of_user1_1, setup.user3),
        Case(f'usr3 good creds + {setup.cert_spif_of_user1_2.info}', setup.user3, setup.cert_spif_of_user1_2, setup.user3),
        Case(f'usr3 good creds + {setup.cert_spif_of_user2.info}', setup.user3, setup.cert_spif_of_user2, setup.user3),
        # usr3 bad pw
        Case(f'usr3 bad creds + no cert', user3_bad_pw, None, None),
        Case(f'usr3 bad creds + {setup.cert_2_spifs.info}', user3_bad_pw, setup.cert_2_spifs, None),
        Case(f'usr3 bad creds + {setup.cert_no_spif.info}', user3_bad_pw, setup.cert_no_spif, None),
        Case(f'usr3 bad creds + {setup.cert_spif_not_exists.info}', user3_bad_pw, setup.cert_spif_not_exists, None),
        Case(f'usr3 bad creds + {setup.cert_spif_of_user1_1.info}', user3_bad_pw, setup.cert_spif_of_user1_1, None),
        Case(f'usr3 bad creds + {setup.cert_spif_of_user1_2.info}', user3_bad_pw, setup.cert_spif_of_user1_2, None),
        Case(f'usr3 bad creds + {setup.cert_spif_of_user2.info}', user3_bad_pw, setup.cert_spif_of_user2, None),
    ]

    mtls_cases: List[Case] = [
        # no creds
        Case('no creds + no cert', None, None, None),
        Case(f'no creds + {setup.cert_2_spifs.info}', None, setup.cert_2_spifs, None),
        Case(f'no creds + {setup.cert_no_spif.info}', None, setup.cert_no_spif, None),
        Case(f'no creds + {setup.cert_spif_not_exists.info}', None, setup.cert_spif_not_exists, None),
        Case(f'no creds + {setup.cert_spif_of_user1_1.info}', None, setup.cert_spif_of_user1_1, setup.user1),
        Case(f'no creds + {setup.cert_spif_of_user1_2.info}', None, setup.cert_spif_of_user1_2, setup.user1),
        Case(f'no creds + {setup.cert_spif_of_user2.info}', None, setup.cert_spif_of_user2, setup.user2),
        # usr1 good pw
        Case(f'usr1 good creds + no cert', setup.user1, None, None),
        Case(f'usr1 good creds + {setup.cert_2_spifs.info}', setup.user1, setup.cert_2_spifs, None),
        Case(f'usr1 good creds + {setup.cert_no_spif.info}', setup.user1, setup.cert_no_spif, None),
        Case(f'usr1 good creds + {setup.cert_spif_not_exists.info}', setup.user1, setup.cert_spif_not_exists, None),
        Case(f'usr1 good creds + {setup.cert_spif_of_user1_1.info}', setup.user1, setup.cert_spif_of_user1_1, setup.user1),
        Case(f'usr1 good creds + {setup.cert_spif_of_user1_2.info}', setup.user1, setup.cert_spif_of_user1_2, setup.user1),
        Case(f'usr1 good creds + {setup.cert_spif_of_user2.info}', setup.user1, setup.cert_spif_of_user2, setup.user2),
        # usr1 bad pw
        Case(f'usr1 bad creds + no cert', user1_bad_pw, None, None),
        Case(f'usr1 bad creds + {setup.cert_2_spifs.info}', user1_bad_pw, setup.cert_2_spifs, None),
        Case(f'usr1 bad creds + {setup.cert_no_spif.info}', user1_bad_pw, setup.cert_no_spif, None),
        Case(f'usr1 bad creds + {setup.cert_spif_not_exists.info}', user1_bad_pw, setup.cert_spif_not_exists, None),
        Case(f'usr1 bad creds + {setup.cert_spif_of_user1_1.info}', user1_bad_pw, setup.cert_spif_of_user1_1, setup.user1),
        Case(f'usr1 bad creds + {setup.cert_spif_of_user1_2.info}', user1_bad_pw, setup.cert_spif_of_user1_2, setup.user1),
        Case(f'usr1 bad creds + {setup.cert_spif_of_user2.info}', user1_bad_pw, setup.cert_spif_of_user2, setup.user2),
        # usr2 good pw
        Case(f'usr2 good creds + no cert', setup.user2, None, None),
        Case(f'usr2 good creds + {setup.cert_2_spifs.info}', setup.user2, setup.cert_2_spifs, None),
        Case(f'usr2 good creds + {setup.cert_no_spif.info}', setup.user2, setup.cert_no_spif, None),
        Case(f'usr2 good creds + {setup.cert_spif_not_exists.info}', setup.user2, setup.cert_spif_not_exists, None),
        Case(f'usr2 good creds + {setup.cert_spif_of_user1_1.info}', setup.user2, setup.cert_spif_of_user1_1, setup.user1),
        Case(f'usr2 good creds + {setup.cert_spif_of_user1_2.info}', setup.user2, setup.cert_spif_of_user1_2, setup.user1),
        Case(f'usr2 good creds + {setup.cert_spif_of_user2.info}', setup.user2, setup.cert_spif_of_user2, setup.user2),
        # usr2 bad pw
        Case(f'usr2 bad creds + no cert', user2_bad_pw, None, None),
        Case(f'usr2 bad creds + {setup.cert_2_spifs.info}', user2_bad_pw, setup.cert_2_spifs, None),
        Case(f'usr2 bad creds + {setup.cert_no_spif.info}', user2_bad_pw, setup.cert_no_spif, None),
        Case(f'usr2 bad creds + {setup.cert_spif_not_exists.info}', user2_bad_pw, setup.cert_spif_not_exists, None),
        Case(f'usr2 bad creds + {setup.cert_spif_of_user1_1.info}', user2_bad_pw, setup.cert_spif_of_user1_1, setup.user1),
        Case(f'usr2 bad creds + {setup.cert_spif_of_user1_2.info}', user2_bad_pw, setup.cert_spif_of_user1_2, setup.user1),
        Case(f'usr2 bad creds + {setup.cert_spif_of_user2.info}', user2_bad_pw, setup.cert_spif_of_user2, setup.user2),
        # usr3 good pw
        Case(f'usr3 good creds + no cert', setup.user3, None, None),
        Case(f'usr3 good creds + {setup.cert_2_spifs.info}', setup.user3, setup.cert_2_spifs, setup.user3),
        Case(f'usr3 good creds + {setup.cert_no_spif.info}', setup.user3, setup.cert_no_spif, setup.user2),
        Case(f'usr3 good creds + {setup.cert_spif_not_exists.info}', setup.user3, setup.cert_spif_not_exists, setup.user2),
        Case(f'usr3 good creds + {setup.cert_spif_of_user1_1.info}', setup.user3, setup.cert_spif_of_user1_1, setup.user1),
        Case(f'usr3 good creds + {setup.cert_spif_of_user1_2.info}', setup.user3, setup.cert_spif_of_user1_2, setup.user1),
        Case(f'usr3 good creds + {setup.cert_spif_of_user2.info}', setup.user3, setup.cert_spif_of_user2, setup.user2),
        # usr3 bad pw
        Case(f'usr3 bad creds + no cert', user3_bad_pw, None, None),
        Case(f'usr3 bad creds + {setup.cert_2_spifs.info}', user3_bad_pw, setup.cert_2_spifs, None),
        Case(f'usr3 bad creds + {setup.cert_no_spif.info}', user3_bad_pw, setup.cert_no_spif, None),
        Case(f'usr3 bad creds + {setup.cert_spif_not_exists.info}', user3_bad_pw, setup.cert_spif_not_exists, None),
        Case(f'usr3 bad creds + {setup.cert_spif_of_user1_1.info}', user3_bad_pw, setup.cert_spif_of_user1_1, setup.user1),
        Case(f'usr3 bad creds + {setup.cert_spif_of_user1_2.info}', user3_bad_pw, setup.cert_spif_of_user1_2, setup.user1),
        Case(f'usr3 bad creds + {setup.cert_spif_of_user2.info}', user3_bad_pw, setup.cert_spif_of_user2, setup.user2),
    ]

    cases_by_security_mode: Dict[str, List[Case]] = {
        SecurityMode.UNSECURED: no_mtls_cases,
        SecurityMode.TLS: no_mtls_cases,
        SecurityMode.MTLS: mtls_cases
    }

    with allure.step('take new revision number for testing admin permissions'):
        revision_num = get_tmp_revision_number_for_test_only()

    with allure.step('test all cases'):
        for mode in SecurityMode.ALL_MODES:
            with allure.independent_step(mode):
                with allure.step(f'setup security mode: {mode}'):
                    setup_api_security_mode(mode, setup.server_cert, setup.server_ca)
                check_test_cases(cases_by_security_mode[mode], setup, mode, revision_num, engines)


def check_spiffe_negative(engines, revision_num, setup, verify_config: bool = True, verify_auth: bool = True, users_should_exist: bool = True):
    if verify_config:
        with allure.step('verify config not kept in show'):
            for user in [setup.user1, setup.user2]:
                with allure.independent_step(user.username):
                    user_obj = System().aaa.user.user_id[user.username]
                    if users_should_exist:
                        out = OutputParsingTool.parse_json_str_to_dictionary(user_obj.spiffe_id.show()).get_returned_value()
                        expected = {}
                        assert out == expected, f'output of {user.username} general spiffe resource is not as expected:\nexpected: {expected}\nactual: {out}'
                    else:
                        user_obj.spiffe_id.show(should_succeed=False)
    if verify_auth:
        with allure.step("verify spiffe auth doesn't work"):
            cases: List[Case] = [
                Case(f'no creds + {setup.cert_spif_of_user1_1.info}', None, setup.cert_spif_of_user1_1, None),
                Case(f'no creds + {setup.cert_spif_of_user1_2.info}', None, setup.cert_spif_of_user1_2, None),
                Case(f'no creds + {setup.cert_spif_of_user2.info}', None, setup.cert_spif_of_user2, None),
            ]
            check_test_cases(cases, setup, SecurityMode.MTLS, revision_num, engines)


def check_spiffe_positive(engines, revision_num, setup, verify_config: bool = True, verify_auth: bool = True):
    if verify_config:
        with allure.step('verify config kept in show'):
            for user in [setup.user1, setup.user2]:
                with allure.independent_step(user.username):
                    user_obj = System().aaa.user.user_id[user.username]
                    out = OutputParsingTool.parse_json_str_to_dictionary(user_obj.spiffe_id.show()).get_returned_value()
                    expected = {spif: {} for spif in setup.spiffes_info[user.username]}
                    assert out == expected, f'output of {user.username} general spiffe resource is not as expected:\nexpected: {expected}\nactual: {out}'
    if verify_auth:
        with allure.step('verify spiffe auth works'):
            cases: List[Case] = [
                Case(f'no creds + {setup.cert_spif_of_user1_1.info}', None, setup.cert_spif_of_user1_1, setup.user1),
                Case(f'no creds + {setup.cert_spif_of_user1_2.info}', None, setup.cert_spif_of_user1_2, setup.user1),
                Case(f'no creds + {setup.cert_spif_of_user2.info}', None, setup.cert_spif_of_user2, setup.user2),
            ]
            check_test_cases(cases, setup, SecurityMode.MTLS, revision_num, engines)


def check_test_cases(cases: List[Case], setup: TestSetup, security_mode, revision_num, engines):
    with allure.step('check cases'):
        for case in cases:
            with allure.independent_step(case.name):
                client_ca = None if security_mode == SecurityMode.UNSECURED else setup.server_cert
                case.verify_show(engines.dut.ip, client_ca, security_mode == SecurityMode.UNSECURED, build_curl_cmd_and_verify)
                case.verify_set(revision_num, engines.dut.ip, client_ca, security_mode == SecurityMode.UNSECURED, build_curl_set_cmd_and_verify)


@pytest.mark.track_serial_console
@pytest.mark.security
@pytest.mark.mtls
@pytest.mark.parametrize('reboot_flow', random.sample(RebootTestFlowType.ALL_TYPES, 1))
def test_api_spiffe_reboot_case(reboot_flow, engines, scp_player, dut_hostname):
    """
    Verify that saved SPIFFE config kept after reboot

    Steps:
    1. Set mtls mode
    2. Save
    3. Configure spiffe to user
    4. Don't save
    5. Reboot
    6. Verify config not kept in show
    7. Verify mtls passwordless connection using spiffe doesn't work
    """

    is_save_flow = reboot_flow == RebootTestFlowType.WITH_SAVE

    with allure.step('take new revision number for testing admin permissions'):
        revision_num = get_tmp_revision_number_for_test_only()

    with allure.step('setup'):
        with allure.step('prepare certs, users, spiffes'):
            setup: TestSetup = setup_test(dut_hostname, engines, scp_player, not is_save_flow)

        with allure.step(f'setup security mode: {SecurityMode.MTLS}'):
            setup_api_security_mode(SecurityMode.MTLS, setup.server_cert, setup.server_ca)

    if is_save_flow:
        with allure.step('save config'):
            NvueGeneralCli.save_config(engines.dut)

    with allure.step('reboot the system'):
        NvCommand().system.action_reboot(flags='force').verify_result()
        engines.dut.disconnect()

    with allure.step('verify after reboot'):

        if is_save_flow:
            check_spiffe_positive(engines, revision_num, setup)
        else:
            check_spiffe_negative(engines, revision_num, setup)


def api_spiffe_factory_reset_no_params_check():
    """
    Factory reset [no params, keep only-files]

    Verify that saved SPIFFE config is removed after these reset factory flavors
    """

    engines = TestToolkit.engines
    scp_player = get_scp_player(engines)
    dut_hostname = get_dut_hostname(engines)

    with allure.step('setup'):
        with allure.step('prepare certs, users, spiffes'):
            setup: TestSetup = setup_test(dut_hostname, engines, scp_player)

        with allure.step(f'setup security mode: {SecurityMode.MTLS}'):
            setup_api_security_mode(SecurityMode.MTLS, setup.server_cert, setup.server_ca)

        with allure.step('save config'):
            NvueGeneralCli.save_config(engines.dut)

    yield  # factory reset

    try:
        with allure.step('verify after factory reset'):
            with allure.step('take new revision number for testing admin permissions'):
                revision_num = get_tmp_revision_number_for_test_only()

            check_spiffe_negative(engines, revision_num, setup, users_should_exist=False)
    finally:
        cleanup_spiffe(spiffe_setup_users(setup))

    yield  # to prevent StopIteration on the 2nd next() call


def api_spiffe_factory_reset_keep_basic_check():
    """
    Factory reset [keep basic]

    Keep basic – keep everything under aaa/user (spiffe included)
    * api config not kept

    Verify that saved SPIFFE config is removed after these reset factory flavors

    Steps:
    1. Set mtls
    2. Set SPIFFE
    3. Save
    4. Do factory reset
    5. Verify SPIFFE config kept in show (mtls might not be kept)
    6. Verify mtls passwordless connection using spiffe doesn't work
    7. Set mtls again
    8. Verify mtls passwordless connection using spiffe works
    """

    engines = TestToolkit.engines
    scp_player = get_scp_player(engines)
    dut_hostname = get_dut_hostname(engines)

    with allure.step('setup'):
        with allure.step('prepare certs, users, spiffes'):
            setup: TestSetup = setup_test(dut_hostname, engines, scp_player)

        with allure.step(f'setup security mode: {SecurityMode.MTLS}'):
            setup_api_security_mode(SecurityMode.MTLS, setup.server_cert, setup.server_ca)

        with allure.step('save config'):
            NvueGeneralCli.save_config(engines.dut)

    yield  # factory reset

    try:
        with allure.step('verify after factory reset'):
            with allure.step('take new revision number for testing admin permissions'):
                revision_num = get_tmp_revision_number_for_test_only()

            check_spiffe_positive(engines, revision_num, setup, verify_auth=False)
            check_spiffe_negative(engines, revision_num, setup, verify_config=False)

            with allure.step(f'setup security mode again: {SecurityMode.MTLS}'):
                import_certs_safely([setup.server_cert], scp_player)
                import_cas_safely([setup.server_ca], scp_player)
                setup_api_security_mode(SecurityMode.MTLS, setup.server_cert, setup.server_ca)

            check_spiffe_positive(engines, revision_num, setup, verify_config=False)
    finally:
        cleanup_spiffe(spiffe_setup_users(setup))

    yield  # to prevent StopIteration on the 2nd next() call


def api_spiffe_factory_reset_keep_all_config_check():
    """
    Factory reset – keep all config

    Keep all config – removes everything except for saved configuration
    """

    engines = TestToolkit.engines
    scp_player = get_scp_player(engines)
    dut_hostname = get_dut_hostname(engines)

    with allure.step('setup'):
        with allure.step('prepare certs, users, spiffes'):
            setup: TestSetup = setup_test(dut_hostname, engines, scp_player)

        with allure.step(f'setup security mode: {SecurityMode.MTLS}'):
            setup_api_security_mode(SecurityMode.MTLS, setup.server_cert, setup.server_ca)

        with allure.step('save config'):
            NvueGeneralCli.save_config(engines.dut)

    yield  # factory reset

    try:
        with allure.step('verify after factory reset'):
            with allure.step('take new revision number for testing admin permissions'):
                revision_num = get_tmp_revision_number_for_test_only(CertInfo('', '',
                                                                              setup.cert_spif_of_user1_1.private,
                                                                              setup.cert_spif_of_user1_1.public,
                                                                              '', '',
                                                                              setup.cert_spif_of_user1_1.ip,
                                                                              setup.cert_spif_of_user1_1.ip,
                                                                              setup.server_cert.cacert))

            check_spiffe_positive(engines, revision_num, setup)
    finally:
        cleanup_spiffe(spiffe_setup_users(setup))

    yield  # to prevent StopIteration on the 2nd next() call


def api_spiffe_upgrade_check():
    """
    Upgrade case

    Verify that saved SPIFFE config is kept after upgrade
    """

    engines = TestToolkit.engines
    scp_player = get_scp_player(engines)
    dut_hostname = get_dut_hostname(engines)

    with allure.step('setup'):
        with allure.step('prepare certs, users, spiffes'):
            setup: TestSetup = setup_test(dut_hostname, engines, scp_player)

        with allure.step(f'setup security mode: {SecurityMode.MTLS}'):
            setup_api_security_mode(SecurityMode.MTLS, setup.server_cert, setup.server_ca)

        with allure.step('save config'):
            NvueGeneralCli.save_config(engines.dut)

    yield  # do upgrade

    try:
        with allure.step('verify after upgrade'):
            with allure.step('take new revision number for testing admin permissions'):
                cert = setup.cert_no_spif.copy()
                cert.update(cacert=setup.server_cert.cacert)
                revision_num = get_tmp_revision_number_for_test_only(client_certs=cert)

            check_spiffe_positive(engines, revision_num, setup)
    finally:
        cleanup_spiffe(spiffe_setup_users(setup))

    yield  # to prevent StopIteration on the 2nd next() call
