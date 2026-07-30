import logging
from collections.abc import Generator

import pytest

from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.general.security.api_server.mtls.spiffe_id.test_api_mtls_spiffe_id import (
    api_spiffe_factory_reset_keep_basic_check,
    api_spiffe_factory_reset_no_params_check,
)
from ngts.tests_nvos.general.security.centralized_tests.factory_reset.constants import (
    FACTORY_RESET_TYPE_TO_ACTION_PARAM,
    FactoryResetType,
)
from ngts.tests_nvos.general.security.centralized_tests.helpers.checker_skip_rules import (
    CheckerSkipRule,
    SkipCheckerBySetup,
    should_skip_checker,
)
from ngts.tests_nvos.general.security.certificate.helpers import delete_certificates, delete_crl
from ngts.tests_nvos.general.security.certificate.test_cert_cacert_mgmt import (
    certs_mgmt_factory_reset_keep_only_files_check,
    certs_mgmt_factory_reset_no_params_check,
)
from ngts.tests_nvos.general.security.crl.test_crl import crl_factory_reset_keep_only_files_check
from ngts.tests_nvos.general.security.nv_bridge.helpers import nv_bridge_internal_factory_reset_no_params_check
from ngts.tests_nvos.general.security.gnmi_server.mtls.spiffe_id.test_gnmi_server_spiffe_id import (
    gnmi_spiffe_factory_reset_keep_basic_check,
    gnmi_spiffe_factory_reset_no_params_check,
)
from ngts.tests_nvos.general.security.rbac.nmx_rbac_factory_reset import (
    nmx_controller_rbac_factory_reset_no_params_check,
    nmx_telemetry_rbac_factory_reset_no_params_check,
)
from ngts.tests_nvos.general.security.rbac.rbac_factory_reset import (
    rbac_factory_reset_keep_roles,
    rbac_factory_reset_no_params_check,
)
from ngts.tests_nvos.general.security.sed.helpers import sed_password_factory_reset_check
from ngts.tests_nvos.general.security.test_ssh_cert_auth.helpers import (
    ssh_cert_auth_factory_reset_keep_only_files_check,
    ssh_cert_auth_factory_reset_no_params_check,
)
from ngts.tests_nvos.general.security.test_ssh_pka.test_ssh_pka_pka_only import (
    ssh_pka_factory_reset_keep_basic_check,
    ssh_pka_factory_reset_no_params_check,
)
from ngts.tests_nvos.general.security.tpm_attestation.helpers import tpm_attestation_factory_reset_no_params_check
from ngts.tests_nvos.general.upgrade_downgrade.feature_checkers import (
    Skipped,
    _check_api_mtls_spiffe_id_and_crl,
    _check_cert_mgmt,
    _check_gnmi_mtls_spiffe_id_and_crl,
    _check_nmx_controller_rbac,
    _check_nmx_telemetry_rbac,
    _check_rbac,
    _check_sed_password,
    _check_ssh_cert_auth,
)
from ngts.tests_nvos.system.factory_reset.helpers import update_timezone
from ngts.tools.test_utils import allure_utils as allure


def _wrap_feature_checker(checker_func) -> Generator[None, None, None]:
    """Wrap a feature_checker function for use in factory reset tests."""
    engines = TestToolkit.engines
    devices = TestToolkit.devices
    version_to_run_all = "25.04.9999"
    yield from checker_func(engines=engines, devices=devices, sys_min_ver=version_to_run_all, sys_max_ver=version_to_run_all)


TPM_ATTESTATION = "TPM attestation"
CERTS_MGMT = "Certificates management"
SED_PASSWORD = "SED password"
SSH_PKA = "SSH PKA"
RBAC = "RBAC"
CRL = "CRL"
SSH_CERT_AUTH = "SSH cert auth"
API_SPIFFE_ID = "API SPIFFE ID + CRL"
GNMI_SPIFFE_ID = "GNMI SPIFFE ID + CRL"
NMX_CONTROLLER_RBAC = "NMX Controller RBAC"
NMX_TELEMETRY_RBAC = "NMX Telemetry RBAC"
NV_BRIDGE_INTERNAL = "NV Bridge Internal"

CHECKERS_SKIP_RULES: dict[str, CheckerSkipRule] = {
    # NMX checkers only run on Juliet and Rosalind
    NMX_CONTROLLER_RBAC: SkipCheckerBySetup(["juliet", "rosalind"], False),
    NMX_TELEMETRY_RBAC: SkipCheckerBySetup(["juliet", "rosalind"], False),
    # SED and TPM not supported on gorilla
    SED_PASSWORD: SkipCheckerBySetup(["gorilla"]),
    TPM_ATTESTATION: SkipCheckerBySetup(["gorilla"]),
    # SSH CERT AUTH not supported on juliet and rosalind
    SSH_CERT_AUTH: SkipCheckerBySetup(["juliet", "rosalind"], True),
    # NV Bridge internal encryption is an NMX/cluster feature; standalone IB
    # switches (croc QM3400, mamba QM3000) have no `system internal`, so
    # system.internal is absent and the checker raises AttributeError. Skip it
    # on those setups.
    NV_BRIDGE_INTERNAL: SkipCheckerBySetup(["croc", "mamba"]),
}

NO_PARAMS_CHECKERS: dict[str, Generator[None, None, None]] = {
    TPM_ATTESTATION: tpm_attestation_factory_reset_no_params_check(),
    CERTS_MGMT: certs_mgmt_factory_reset_no_params_check(),
    SSH_PKA: ssh_pka_factory_reset_no_params_check(),
    SED_PASSWORD: sed_password_factory_reset_check(),
    RBAC: rbac_factory_reset_no_params_check(),
    # Comprehensive SPIFFE checkers (replaces deprecated GNMI_CERT and API_MTLS)
    API_SPIFFE_ID: api_spiffe_factory_reset_no_params_check(),
    GNMI_SPIFFE_ID: gnmi_spiffe_factory_reset_no_params_check(),
    # NMX RBAC checkers (replaces deprecated NMX_CERT for RBAC)
    NMX_CONTROLLER_RBAC: nmx_controller_rbac_factory_reset_no_params_check(),
    NMX_TELEMETRY_RBAC: nmx_telemetry_rbac_factory_reset_no_params_check(),
    # SSH certificate authentication
    SSH_CERT_AUTH: ssh_cert_auth_factory_reset_no_params_check(),
    # NV Bridge internal encryption
    NV_BRIDGE_INTERNAL: nv_bridge_internal_factory_reset_no_params_check(),
}

KEEP_BASIC_CHECKERS: dict[str, Generator[None, None, None]] = {
    CERTS_MGMT: certs_mgmt_factory_reset_no_params_check(),
    SSH_PKA: ssh_pka_factory_reset_keep_basic_check(),
    SED_PASSWORD: sed_password_factory_reset_check(),
    RBAC: rbac_factory_reset_keep_roles(),
    # Comprehensive SPIFFE checkers (replaces deprecated GNMI_CERT and API_MTLS)
    API_SPIFFE_ID: api_spiffe_factory_reset_keep_basic_check(),
    GNMI_SPIFFE_ID: gnmi_spiffe_factory_reset_keep_basic_check(),
    # NMX RBAC checkers - config not kept in keep-basic
    NMX_CONTROLLER_RBAC: nmx_controller_rbac_factory_reset_no_params_check(),
    NMX_TELEMETRY_RBAC: nmx_telemetry_rbac_factory_reset_no_params_check(),
}

KEEP_ONLY_FILES_CHECKERS: dict[str, Generator[None, None, None]] = {
    CERTS_MGMT: certs_mgmt_factory_reset_keep_only_files_check(),
    SED_PASSWORD: sed_password_factory_reset_check(),
    RBAC: rbac_factory_reset_no_params_check(),
    # Comprehensive SPIFFE checkers (replaces deprecated GNMI_CERT and API_MTLS)
    API_SPIFFE_ID: api_spiffe_factory_reset_no_params_check(),
    GNMI_SPIFFE_ID: gnmi_spiffe_factory_reset_no_params_check(),
    # NMX RBAC checkers - config not kept
    NMX_CONTROLLER_RBAC: nmx_controller_rbac_factory_reset_no_params_check(),
    NMX_TELEMETRY_RBAC: nmx_telemetry_rbac_factory_reset_no_params_check(),
    # SSH certificate authentication - verify keys file preserved
    SSH_CERT_AUTH: ssh_cert_auth_factory_reset_keep_only_files_check(),
    # CRL - verify CRL files preserved but config reset
    CRL: crl_factory_reset_keep_only_files_check(),
}

KEEP_ALL_CONFIG_CHECKERS: dict[str, Generator[None, None, None]] = {
    CERTS_MGMT: _wrap_feature_checker(_check_cert_mgmt),
    SED_PASSWORD: _wrap_feature_checker(_check_sed_password),
    RBAC: _wrap_feature_checker(_check_rbac),
    SSH_CERT_AUTH: _wrap_feature_checker(_check_ssh_cert_auth),
    API_SPIFFE_ID: _wrap_feature_checker(_check_api_mtls_spiffe_id_and_crl),
    GNMI_SPIFFE_ID: _wrap_feature_checker(_check_gnmi_mtls_spiffe_id_and_crl),
    NMX_CONTROLLER_RBAC: _wrap_feature_checker(_check_nmx_controller_rbac),
    NMX_TELEMETRY_RBAC: _wrap_feature_checker(_check_nmx_telemetry_rbac),
}

FACTORY_RESET_TYPE_TO_CHECKER_FUNCTIONS: dict[str, dict[str, Generator[None, None, None]]] = {
    FactoryResetType.NO_PARAMS: NO_PARAMS_CHECKERS,
    FactoryResetType.KEEP_BASIC: KEEP_BASIC_CHECKERS,
    FactoryResetType.KEEP_ONLY_FILES: KEEP_ONLY_FILES_CHECKERS,
    FactoryResetType.KEEP_ALL_CONFIG: KEEP_ALL_CONFIG_CHECKERS,
}


@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.security
@pytest.mark.reset_factory
@pytest.mark.parametrize("factory_reset_type", FactoryResetType.ALL_TYPES)
def test_reset_factory(factory_reset_type, engines, devices, topology_obj, platform_params, setup_name):
    """
    Validate reset factory flavors
    """
    checkers = FACTORY_RESET_TYPE_TO_CHECKER_FUNCTIONS[factory_reset_type]
    if not checkers:
        pytest.skip("test skipped: no checkers registered for this test")
    checkers = {name: checker for name, checker in checkers.items() if not should_skip_checker(CHECKERS_SKIP_RULES, name, setup_name)}
    if not checkers:
        pytest.skip("test skipped: no checkers registered for this test")

    action_flag = FACTORY_RESET_TYPE_TO_ACTION_PARAM[factory_reset_type]
    logging.info(f"checkers names for factory reset {factory_reset_type}: {list(checkers.keys())}")
    logging.info(f'action flag for factory reset {factory_reset_type}: "{action_flag}"')

    should_do_factory_reset = False

    active_checkers: list[tuple[str, Generator[None, None, None]]] = [
        (name, checker) for name, checker in checkers.items() if not should_skip_checker(CHECKERS_SKIP_RULES, name, setup_name)
    ]

    successful_pre_checkers: list[tuple[str, Generator[None, None, None]]] = []
    failed_pre_checkers: dict[str, str] = {}

    try:
        with allure.step("setup"):
            pass

        with allure.step(f"test: factory reset {factory_reset_type}"):
            with allure.independent_step("pre factory reset steps"):
                for name, checker in active_checkers:
                    with allure.independent_step(name):
                        try:
                            next(checker)
                            successful_pre_checkers.append((name, checker))
                            should_do_factory_reset = True
                        except Skipped as e:
                            reason = f"Skipped: {e.reason}"
                            logging.info("Checker %s - %s", name, reason)
                            failed_pre_checkers[name] = reason

            if should_do_factory_reset:
                with allure.step(f"Run reset factory - {factory_reset_type}"):
                    do_factory_reset(devices, action_flag, topology_obj)

                with allure.step("post factory reset steps"):
                    for name, reason in failed_pre_checkers.items():
                        with allure.independent_step(name):
                            with allure.step(f"Pre-checker {reason} - not running post-check"):
                                logging.info("Checker %s - Pre-checker %s, skipping post-check", name, reason)

                    for name, checker in successful_pre_checkers:
                        with allure.independent_step(name):
                            try:
                                next(checker)
                                logging.info("Checker %s finished successfully", name)
                            except StopIteration:
                                logging.info("Checker %s finished (one-yield convention)", name)
                                with allure.step("Checker completed (one-yield convention)"):
                                    pass

    finally:
        with allure.step("cleanup"):
            with allure.independent_step("delete ca/certs/crls"):
                delete_certificates()
                delete_certificates(True)
                delete_crl()


def do_factory_reset(devices, flag, topology_obj):
    system = System()
    with allure.step("do factory reset"):
        system.factory_default.action_reset(
            operation=devices.dut.reset_factory,
            param=flag,
            topology_obj=topology_obj,
            system_is_ready_timeout=devices.dut.timeout_system_is_ready,
            verify_duration=False,
        ).verify_result()
    with allure.step("update timezone"):
        update_timezone(system)
