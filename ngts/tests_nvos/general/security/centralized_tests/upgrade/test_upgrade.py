from typing import Dict, Generator

import pytest
import retry

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import ImageConsts
from ngts.nvos_tools.infra.DutUtilsTool import RebootParams
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.general.security.centralized_tests.helpers.checker_skip_rules import CheckerSkipRule, \
    SkipCheckerByCond, SkipCheckerBySetup, should_skip_checker
from ngts.tests_nvos.general.security.certificate.helpers import delete_certificates
from ngts.tests_nvos.general.security.certificate.test_cert_cacert_mgmt import certs_mgmt_upgrade_check
from ngts.tests_nvos.general.security.crl.test_crl import crl_factory_reset_keep_all_config_check
from ngts.tests_nvos.general.security.nmx_rbac.test_nmx_rbac import nmx_rbac_upgrade_check
from ngts.tests_nvos.general.security.nmx_cert.test_cluster_app_mngr_security import \
    cluster_app_mngr_security_upgrade_check
from ngts.tests_nvos.general.security.ima.ima_upgrade import ima_upgrade_check
from ngts.tests_nvos.general.security.test_api_server_security.test_api_mtls import api_mtls_upgrade_check
from ngts.tests_nvos.general.upgrade_downgrade.feature_checkers import Skipped
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.system.factory_reset.helpers import *
from ngts.tests_nvos.system.gnmi.helpers import get_scp_player
from ngts.tests_nvos.system.gnmi.test_gnmi_mtls import gnmi_mtls_upgrade_check
from ngts.tools.test_utils import allure_utils as allure

TPM_ATTESTATION = 'TPM attestation'
GNMI_CERT = 'GNMI cert + mTLS'
NMX_CERT = 'NMX cert'
API_MTLS = 'API mTLS'
SED_PASSWORD = 'SED password'
CERTS_MGMT = 'Certificates management'
CRL = 'CRL'
NMX_RBAC = 'NMX RBAC'
IMA = 'IMA'

UPGRADE_CHECKERS: Dict[str, Generator[None, None, None]] = {
    GNMI_CERT: gnmi_mtls_upgrade_check(),
    NMX_CERT: cluster_app_mngr_security_upgrade_check(),
    API_MTLS: api_mtls_upgrade_check(),
    CERTS_MGMT: certs_mgmt_upgrade_check(),
    CRL: crl_factory_reset_keep_all_config_check(),
    NMX_RBAC: nmx_rbac_upgrade_check(),
    IMA: ima_upgrade_check(),
}

CHECKERS_SKIP_RULES: Dict[str, CheckerSkipRule] = {
    API_MTLS: SkipCheckerByCond(is_bug_active(4103432)),  # TODO: remove once bug #4103432 closed
    NMX_CERT: SkipCheckerBySetup(['juliet'], False),
    SED_PASSWORD: SkipCheckerBySetup(['gorilla']),
    TPM_ATTESTATION: SkipCheckerBySetup(['gorilla']),
    CRL: SkipCheckerBySetup(['juliet'], False),
}


@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.security
@pytest.mark.upgrade
def test_downgrade_upgrade(base_version_realpath, target_version_realpath, devices, engines, topology_obj, setup_name):
    """
    Validate upgrade scenario
    """

    checkers = UPGRADE_CHECKERS
    if not checkers:
        pytest.skip('test skipped: no checkers registered for this test')
    logging.info(f'checkers names for upgrade: {list(checkers.keys())}')
    checkers = {name: checker for name, checker in checkers.items() if
                not should_skip_checker(CHECKERS_SKIP_RULES, name, setup_name)}
    if not checkers:
        pytest.skip('test skipped: no checkers registered for this test')

    system = System()
    target_version_name = target_version_realpath.split("/")[-1]
    need_recovery = False
    successful_pre_checkers: list[tuple[str, Generator[None, None, None]]] = []
    skipped_pre_checkers: dict[str, str] = {}

    try:
        with allure.step('setup'):
            with allure.step('verify cur version is target version'):
                is_cur_version_as_expected(system, target_version_realpath).verify_result()
            with allure.step(f'downgrade to base version: {base_version_realpath}'):
                with allure.step('install base version'):
                    fetch_install_img(system, base_version_realpath, engines)
                    need_recovery = True
                with allure.step('uninstall orig version'):
                    system.image.action_uninstall('force')

        with allure.step(f'test'):
            with allure.independent_step('pre upgrade steps'):
                for name, checker in checkers.items():
                    if not should_skip_checker(CHECKERS_SKIP_RULES, name, setup_name):
                        with allure.independent_step(name):
                            try:
                                next(checker)
                                successful_pre_checkers.append((name, checker))
                            except Skipped as exc:
                                skipped_pre_checkers[name] = exc.reason
                                logging.info('Checker %s skipped: %s', name, exc.reason)

            with allure.step(f"Run upgrade: {target_version_name}"):
                fetch_install_img(system, target_version_realpath, engines)

            with allure.step('post upgrade steps'):
                for name, reason in skipped_pre_checkers.items():
                    with allure.independent_step(name):
                        with allure.step(f'Skipped: {reason}'):
                            logging.info('Checker %s skipped in pre-upgrade - not running post-check', name)

                for name, checker in successful_pre_checkers:
                    with allure.independent_step(name):
                        try:
                            next(checker)
                        except StopIteration:
                            logging.info('Checker %s finished (one-yield convention)', name)

    finally:
        with allure.step('cleanup'):
            if need_recovery:
                if is_cur_version_as_expected(system, target_version_realpath).result:
                    with allure.step('uninstall base version'):
                        system.image.action_uninstall('force')
                else:
                    with allure.step('recovery: manufacture to target (orig) version'):
                        NvueGeneralCli(engines.dut, devices.dut).install_image_via_onie(topology_obj,
                                                                                        target_version_realpath)
            with allure.independent_step('delete fetched images'):
                system.image.files.delete_all_existing_files()
            with allure.independent_step('delete ca/certs'):
                delete_certificates()
                delete_certificates(True)


def is_cur_version_as_expected(system: System, expected_version: str) -> ResultObj:
    expected_version = expected_version.split('/')[-1].replace('.bin', '').replace('arm64-', '').replace('amd64-', '')
    cur_version = system.version.get_nvos_image_version()
    with allure.step(f'check if {expected_version} (orig) == {cur_version} (cur)'):
        res = expected_version == cur_version
        return ResultObj(res,
                         f'cur version is {"" if res else "not "}as expected.\nexpected: {expected_version}\nactual: {cur_version}')


def fetch_install_img(system: System, img_path: str, engines, *, install_timeout=None):
    @retry.retry(Exception, tries=3, delay=1)
    def _fetch_img_with_retry(scp_url):
        system.image.action_fetch(scp_url, base_url='')

    img_name = img_path.split("/")[-1]
    with allure.step(f"fetch image: {img_name}"):
        scp_player = get_scp_player(engines)
        scp_url = ImageConsts.SCP_PATH_SERVER.format(username=scp_player.username, password=scp_player.password,
                                                     ip=scp_player.ip, path=img_path)
        _fetch_img_with_retry(scp_url)
    with allure.step(f'install image: {img_name}'):
        system.image.files.file_name[img_name].action_install(reboot_params=RebootParams(), timeout=install_timeout)
    with allure.step('disconnect dut engine'):
        engines.dut.disconnect()
