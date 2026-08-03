import logging
import os
import random
import string
from typing import List, Dict, Optional, Tuple

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import CacertType
from ngts.nvos_tools.infra.CertificateGenerator import CertificateGenerator
from ngts.nvos_tools.infra.CmdRunner import CmdRunner
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.constants import ETC_HOSTS, TMP_TEST_CERTS_DIR, YEAR
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tests_nvos.helpers.general_helpers import generate_rand_str
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_general_utils import generate_scp_uri_using_player

logger = logging.getLogger(__name__)


def set_new_random_users(num_users: int, role: str, apply=False, engine=None) -> List[UserInfo]:
    """Create random local AAA users.

    ``engine`` should be the caller's DUT engine (e.g. ``engines.dut``). Falls back to
    ``TestToolkit.get_engine()`` only for legacy call sites / session fixtures.
    """
    system = System()
    dut_engine = engine if engine is not None else TestToolkit.get_engine()
    users = []
    with allure.step(f'set {num_users} new random local {role} users'):
        for _ in range(num_users):
            username, password = system.aaa.user.set_new_user(role=role)
            users.append(UserInfo(username, password, role))
    if apply:
        with allure.step('apply users'):
            system._general_cli_wrapper.apply_config(dut_engine, verify_execution=True)
    return users


def cleanup_aaa_users(users: Optional[List[UserInfo]], engine=None) -> None:
    """Best-effort batch removal of AAA users created by SPIFFE/gNMI setup.

    Stages all unsets with ``apply=False``, then applies once. On apply failure,
    detaches the pending revision so staged deletes cannot poison later tests.
    Empty/no-diff applies are skipped via ``skip_no_config_diff_err``.

    Prefer passing ``engine`` (e.g. ``engines.dut``). Falls back to
    ``TestToolkit.get_engine()`` only for session-scoped cleanup without an engines fixture.
    """
    if not users:
        return
    system = System()
    dut_engine = engine if engine is not None else TestToolkit.get_engine()
    cleanup_staged = False
    with allure.step('cleanup AAA users created by test setup'):
        for user in users:
            with allure.step(f'unset user {user.username}'):
                try:
                    system.aaa.user.user_id[user.username].unset(apply=False).verify_result()
                    cleanup_staged = True
                except Exception as error:
                    logger.warning('Failed to unset AAA user "%s": %s', user.username, error)
        if not cleanup_staged:
            return
        with allure.step('apply AAA user cleanup'):
            try:
                system._general_cli_wrapper.apply_config(
                    dut_engine, verify_execution=True, skip_no_config_diff_err=True)
            except Exception as error:
                logger.warning('Failed to apply AAA user cleanup: %s', error)
                with allure.step('detach pending AAA cleanup revision after apply failure'):
                    try:
                        NvueGeneralCli.detach_config(dut_engine)
                    except Exception as detach_error:
                        logger.warning(
                            'Failed to detach pending AAA cleanup revision: %s', detach_error)


def add_etc_host_mapping_to_dn(dn, address, cmd_runner=None):
    cmd_runner = cmd_runner or CmdRunner()
    cmd_runner.run_cmd(f'echo "{address} {dn}" | sudo tee -a {ETC_HOSTS}')


def remove_etc_host_mapping_to_dn(dn, cmd_runner=None):
    cmd_runner = cmd_runner or CmdRunner()
    # cmd = f"sudo sed -i '/{dn}/d' {ETC_HOSTS}"
    cmd = f"cp -f {ETC_HOSTS} /tmp/hosts.new && sed -i '/{dn}/d' /tmp/hosts.new && sudo tee {ETC_HOSTS} < /tmp/hosts.new && rm -f /tmp/hosts.new"
    cmd_runner.run_cmd(cmd)


def prepare_tmp_test_certs(cert_names: List[str], dest_dir, engines, dut_hostname, dut_ip=None,
                           create_chain: bool = False, expiration_days: Optional[int] = None) -> Dict[str, CertInfo]:
    """Generate leaf certs under ``dest_dir``.

    :param expiration_days: when set, leaf validity is that many days (CA stays long-lived).
    """
    certs_info: Dict[str, CertInfo] = {}

    with allure.step('arrange test certs'):
        os.makedirs(dest_dir, exist_ok=True)
        for cert_name in cert_names:
            with allure.step(cert_name):
                with allure.step('make dir for cert'):
                    cert_dir = os.path.join(dest_dir, cert_name)
                    os.makedirs(cert_dir, exist_ok=True)
                with allure.step('add cert info'):
                    rand_pass = ''.join([random.choice(string.ascii_lowercase + '0123456789') for _ in range(10)])
                    cert_info = CertInfo(
                        name=cert_name,
                        info=('cert for test' if expiration_days is None
                              else f'cert for test (valid for {expiration_days} day(s))'),
                        private=os.path.join(cert_dir, 'cert.key'),
                        public=os.path.join(cert_dir, 'cert.pem'),
                        p12_bundle=os.path.join(cert_dir, 'cert.p12'),
                        p12_password=rand_pass,
                        dn=dut_hostname,
                        ip=dut_ip or engines.dut.ip,
                        cacert=os.path.join(cert_dir, 'ca.crt'),
                    )
                    certs_info[cert_name] = cert_info
                with allure.step('generate'):
                    if create_chain:
                        cert_info = CertInfo(
                            name=cert_name,
                            info='chain cert for test',
                            private=os.path.join(cert_dir, 'cert.key'),
                            public=os.path.join(cert_dir, 'cert.pem'),
                            p12_bundle=os.path.join(cert_dir, 'chain.p12'),
                            p12_password=rand_pass,
                            dn=dut_hostname,
                            ip=dut_ip or engines.dut.ip,
                            cacert=os.path.join(cert_dir, 'rCA.crt'),
                        )
                        certs_info[cert_name] = cert_info
                        CertificateGenerator().generate_cert_chain(cert_dir, 'cert', cert_info.dn, cert_info.ip, cert_info.dn,
                                                                   cert_dir, 'rCA', cert_info.p12_password)
                    else:
                        CertificateGenerator().generate_cert(
                            cert_dir, 'cert', cert_info.dn, cert_info.ip, cert_info.dn,
                            cert_dir, 'ca', cert_info.p12_password,
                            expiration_days=expiration_days)
        with allure.step('chmod 777'):
            CmdRunner().run_cmd(f'chmod -R 777 {dest_dir}')

    return certs_info


def import_certs_safely(certs: List[CertInfo], scp_player, override_existing=True):
    system = System()

    def __import_cert(cert: CertInfo):
        with allure.step(f'import cert {cert.name}'):
            system.security.certificate.cert_id[cert.name].action_import(
                uri_bundle=generate_scp_uri_using_player(scp_player, cert.p12_bundle),
                passphrase=cert.p12_password).verify_result()

    with allure.step('import test certs'):
        current_certs = OutputParsingTool.parse_json_str_to_dictionary(
            system.security.certificate.show()).get_returned_value()
        for cert in certs:
            if cert.name in current_certs:
                if override_existing:
                    with allure.step(f'cert {cert.name} already exist. delete existing one before import'):
                        system.security.certificate.cert_id[cert.name].action_delete().verify_result()
                    __import_cert(cert)
            else:
                __import_cert(cert)


def import_crl_safely(crl_name: str, crl_path: str, scp_player, override_existing=True):
    system = System()

    def __import_crl(crl_name: str, crl_path: str):
        with allure.step(f'import crl {crl_name}'):
            system.security.crl.crl_id[crl_name].action_import(
                uri=generate_scp_uri_using_player(scp_player, crl_path)
            ).verify_result()

    with allure.step('import test certs'):
        current_crls = OutputParsingTool.parse_json_str_to_dictionary(
            system.security.crl.show()).get_returned_value()
        if crl_name in current_crls:
            if override_existing:
                with allure.step(f'crl {crl_name} already exist. delete existing one before import'):
                    system.security.crl.crl_id[crl_name].action_delete().verify_result()
                __import_crl(crl_name, crl_path)
        else:
            __import_crl(crl_name, crl_path)


def delete_certs_safely(certs: List[CertInfo]):
    system = System()

    with allure.step('delete certs from the system'):
        current_certs = OutputParsingTool.parse_json_str_to_dictionary(
            system.security.certificate.show()).get_returned_value()
        for cert in certs:
            name = cert.name
            if name in current_certs:
                with allure.step(f'delete cert {name}'):
                    system.security.certificate.cert_id[name].action_delete().verify_result()


def delete_all_imported_certs():
    system = System()

    with allure.step('delete certs from the system'):
        current_certs = OutputParsingTool.parse_json_str_to_dictionary(
            system.security.certificate.show()).get_returned_value()
        for cert in current_certs:
            with allure.step(f'delete cert {cert}'):
                system.security.certificate.cert_id[cert].action_delete().verify_result()


def import_cas_safely(cas: List[CertInfo], scp_player, external: bool = False, override_existing: bool = True):
    system = System()

    def __import_ca(ca: CertInfo):
        with allure.step(f'import CA {ca.cacert_name}'):
            system.security.ca_certificate.cert_id[ca.cacert_name].action_import(
                uri=generate_scp_uri_using_player(scp_player, ca.cacert), external=external).verify_result()

    with allure.step('import test certs'):
        current_cas = OutputParsingTool.parse_json_str_to_dictionary(
            system.security.ca_certificate.show()).get_returned_value()
        for ca in cas:
            if ca.cacert_name in current_cas:
                if override_existing:
                    with allure.step(f'ca {ca.cacert_name} already exist. delete existing one before import'):
                        system.security.ca_certificate.cert_id[ca.cacert_name].action_delete().verify_result()
                    __import_ca(ca)
            else:
                __import_ca(ca)


def delete_cas_safely(cas: List[CertInfo]):
    system = System()

    with allure.step('delete certs from the system'):
        current_cas = OutputParsingTool.parse_json_str_to_dictionary(
            system.security.ca_certificate.show()).get_returned_value()
        for ca in cas:
            name = ca.cacert_name
            if name in current_cas:
                with allure.step(f'delete cert {name}'):
                    system.security.ca_certificate.cert_id[name].action_delete().verify_result()


def delete_all_imported_cas():
    system = System()

    with allure.step('delete certs from the system'):
        current_cas = OutputParsingTool.parse_json_str_to_dictionary(
            system.security.ca_certificate.show()).get_returned_value()
        for ca in current_cas:
            with allure.step(f'delete cert {ca}'):
                system.security.ca_certificate.cert_id[ca].action_delete().verify_result()


def get_cert_with_ca_mismatch(certs: List[CertInfo]) -> CertInfo:
    assert len(certs) >= 2, 'provided cert list must contain at least 2 certs'
    cert1: CertInfo = certs[0]
    cert2: CertInfo = certs[1]
    cert_with_mismatch = cert1.copy()
    cert_with_mismatch.cacert = cert2.cacert
    return cert_with_mismatch


def optional_cacert_types() -> list:  # TODO: remove once closed
    # if any(is_bug_active(bug) for bug in [4251992, 4237677, 4237752, 4237850]):
    #     return [CacertType.GLOBAL]
    # else:
    return CacertType.ALL_TYPES


def get_test_certs_dir_location(certs_dirname_prefix, dut_hostname):
    certs_dirname = f'{certs_dirname_prefix}_{dut_hostname}_{random.randint(0, 9999)}'
    return os.path.join(TMP_TEST_CERTS_DIR, certs_dirname)


def setup_certs_for_tests(certs_dirname_prefix: str, certs_names: List[str], engines, dut_hostname, import_to_dut=False,
                          scp_player=None, dut_ip=None, import_cas=False, create_chain=False) -> Tuple[str, List[CertInfo]]:
    with allure.step('prepare temp test certs in shared location'):
        certs_location = get_test_certs_dir_location(certs_dirname_prefix, dut_hostname)
        if dut_ip and IpTool.is_address_ipv6(dut_ip):
            certs_names = [cert_name if 'ipv6' in cert_name else f'{cert_name}-ipv6' for cert_name in certs_names]
        certs_info: Dict[str, CertInfo] = prepare_tmp_test_certs(certs_names, certs_location, engines, dut_hostname,
                                                                 dut_ip, create_chain)
        certs = list(certs_info.values())
    if import_to_dut:
        with allure.step('import certs to dut'):
            import_certs_safely(list(certs_info.values()), scp_player)
        if import_cas:
            with allure.step('import cas to dut'):
                import_cas_safely(list(certs_info.values()), scp_player)

    return certs_location, certs


def cleanup_certs_for_tests(tmp_certs_dir: str, certs: List[CertInfo], cas: List[CertInfo] = None):
    with allure.step('delete certs from dut'):
        delete_certs_safely(certs)
    if cas:
        with allure.step('delete cas from dut'):
            delete_cas_safely(cas)
    with allure.step('remove temp test certs from shared location'):
        CmdRunner().run_cmd(f'rm -rf {tmp_certs_dir}')


def generate_certs(dest, certs: List[CertInfo], ca_private=None, ca_public=None):
    """
    generate certificates in a given destination directory (issued by the same CA).

    if the directory doesn't exist, we create it anyway
    if CA info not fully given, we generate a new one under given dest dir
    """
    with allure.step(f'create dest dir: {dest}'):
        os.makedirs(dest, exist_ok=True)
    if not (ca_private and ca_public):
        ca_dir = os.path.join(dest, 'ca')
        with allure.step(f'create new ca in dest: {ca_dir}'):
            with allure.step(f'create ca subdir: {ca_dir}'):
                os.makedirs(ca_dir, exist_ok=True)
            with allure.step('generate ca'):
                ca_private, ca_public = CertificateGenerator().generate_ca(ca_dir, 'ca', 10 * YEAR)
    with allure.step('generate certs from that ca'):
        for cert in certs:
            with allure.step(cert.name):
                with allure.step('make dir for cert'):
                    cert_dir = os.path.join(dest, cert.name)
                    os.makedirs(cert_dir, exist_ok=True)
                with allure.step('add info to cert'):
                    cert.update(
                        private=os.path.join(cert_dir, 'cert.key'),
                        public=os.path.join(cert_dir, 'cert.pem'),
                        p12_bundle=os.path.join(cert_dir, 'cert.p12'),
                        p12_password=generate_rand_str(10),
                        cacert=ca_public
                    )
                with allure.step('generate cert'):
                    CertificateGenerator().generate_cert(cert_dir, 'cert', cert.name, cert.ip, cert.dn,
                                                         p12_pass=cert.p12_password, existing_ca_public=ca_public,
                                                         existing_ca_private=ca_private, san_uris=cert.san_uris)
