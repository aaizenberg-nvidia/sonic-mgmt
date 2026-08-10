"""
System feature verification module for NVOS.

This module provides a framework for creating and running feature checkers
that verify system functionality across different operations. Each checker
is designed to validate a specific feature or functionality of the system.

The checkers are implemented as generator functions with a two-phase execution:
1. Setup phase - configure the system for testing (before the first yield)
2. Verification phase - verify the feature works correctly (after the yield)

The module provides decorators to specify compatibility requirements:
- Device types the checker can run on
- Minimum system version required

Usage:
* Adding a new checker:
    - Create a new function with the @_requires_compatibility decorator
    - Implement the two-phase generator pattern (setup, yield, verification)
    - Add the function to the _CHECKERS list

* Running the checkers:
    ```python
    results_generator = feature_checkers.run_checkers(
        engines=engines,
        devices=devices,
        base=base_package,
        target=target_package
    )

    # Get pre-check results
    pre_check_errors = next(results_generator)

    # Run the operation being tested

    # Get post-check results
    post_check_errors = next(results_generator)
    ```
"""

import functools
import logging
import os
import random
import re
import time
from collections.abc import Callable, Generator
from pathlib import Path
from types import SimpleNamespace


from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.ngts_types import DevicesT, EnginesT, OperationalAppliedT
from ngts.nvos_constants.constants_nvos import (
    ClusterApps,
    ClusterConsts,
    NvosConst,
    PhyRoleConsts,
    RbacConsts,
    SystemConsts,
    TestFlowType,
)
from ngts.nvos_constants import constants_nvos as consts_nv
from ngts.tests_nvos.constants import FW_COMPONENT_SSD
from ngts.nvos_tools.Devices import IbDevice
from ngts.nvos_tools.Devices.cpo.CpoTopology import is_cpo_capable
from ngts.nvos_tools.infra.CertificateGenerator import CertificateGenerator
from ngts.nvos_tools.infra.CrlValidator import ClientConfig, RevokeConfig
from ngts.nvos_tools.infra.InterfaceConfigurationTool import InterfaceConfigurationTool
from ngts.nvos_tools.ib.InterfaceConfiguration import Port, nvos_consts as ib_consts
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.infra.FWComponentsTool import FWComponentsTool
from ngts.nvos_tools.infra.SSDTool import SSDTool
from ngts.nvos_tools.infra.NmxRbacTool import NmxRbacTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.TpmTool import TpmTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.nmx.Apps import ClusterApp
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.system.System import System
from ngts.scripts.sonic_deploy.nvos_only_methods import NvosInstallationSteps
from ngts.tests_nvos.conftest import get_dut_hostname
from ngts.tests_nvos.general.security.api_server.mtls.spiffe_id.helpers import (
    get_tmp_revision_number_for_test_only,
)
from ngts.tests_nvos.general.security.api_server.mtls.spiffe_id.test_api_mtls_spiffe_id import (
    TestSetup,
    setup_test,
)
from ngts.tests_nvos.general.security.api_server.mtls.spiffe_id.test_api_mtls_spiffe_id import (
    check_spiffe_positive as check_spiffe_positive_api,
)
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.certificate.helpers import (
    verify_ca_in_expected_locations,
    verify_cert_in_expected_locations,
)
from ngts.tests_nvos.general.security.certificate.test_cert_cacert_mgmt import (
    setup_cert_mgmt_checker,
)
from ngts.tests_nvos.general.security.crl.helpers import ApiCrlValidator, GnmiCrlValidator
from ngts.tests_nvos.general.security.gnmi_server.mtls.spiffe_id.test_gnmi_server_spiffe_id import (
    check_spiffe_positive as check_spiffe_positive_gnmi,
)
from ngts.tests_nvos.general.security.helpers import (
    cleanup_certs_for_tests,
    generate_certs,
    get_test_certs_dir_location,
    import_cas_safely,
    import_certs_safely,
    import_crl_safely,
    setup_certs_for_tests,
)
from ngts.tests_nvos.general.security.mtls.generic_testing.helpers import get_scp_player, verify_ca_configuration, verify_connection
from ngts.tests_nvos.general.security.nmx_cert.constants import EncryptionMode
from ngts.tests_nvos.general.security.nmx_cert.helpers import (
    enable_cluster,
    run_manager_hello_request,
    verify_manager_show,
)
from ngts.tests_nvos.general.security.nmx_cert.test_cluster_app_mngr_security import (
    setup_cluster_app_mngr_security_checker,
)
from ngts.tests_nvos.general.security.nv_bridge.helpers import (
    build_internal_bridge_cert,
    import_internal_certs,
    verify_cluster_app_internal_cert_files,
    verify_cluster_app_internal_crl_show,
    verify_cluster_app_internal_show,
    verify_cluster_app_internal_spiffe_json,
    verify_system_internal_cert_files,
    verify_system_internal_crl_show,
    verify_system_internal_show,
    verify_system_internal_spiffe_json,
    wait_for_cluster_app_update,
)
from ngts.tests_nvos.general.security.radius.constants import (
    RadiusConsts,
    RadiusPhysicalServer,
    RadiusVmServer,
)
from ngts.tests_nvos.general.security.radius.radius_test_utils import (
    update_radius_server_auth_type,
)
from ngts.tests_nvos.general.security.rbac.helpers import verify_rbac_classes_in_role
from ngts.tests_nvos.general.security.security_test_tools.constants import (
    AaaConsts,
    AddressingType,
    AuthMedium,
    AuthMode,
    UserRole,
)
from ngts.tests_nvos.general.security.security_test_tools.generic_remote_aaa_testing.constants import (
    RemoteAaaType,
)
from ngts.tests_nvos.general.security.security_test_tools.security_test_utils import (
    set_local_users,
    verify_auth_mediums,
)
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.RemoteAaaServerInfo import (
    update_active_aaa_server,
)
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import (
    UserInfo,
)
from ngts.tests_nvos.general.security.tacacs.constants import TacacsDockerServer1
from ngts.tests_nvos.general.security.tacacs.tacacs_test_utils import (
    update_tacacs_server_auth_mode,
)
from ngts.tests_nvos.general.security.test_aaa_ldap.constants import LdapEncryptionModes
from ngts.tests_nvos.general.security.test_aaa_ldap.ldap_servers_info import (
    LdapServersP3,
)
from ngts.tests_nvos.general.security.test_aaa_ldap.ldap_test_utils import (
    update_ldap_encryption_mode,
)
from ngts.tests_nvos.general.security.test_api_server_security.constants import API_INSTALLED
from ngts.tests_nvos.general.security.test_api_server_security.helpers import cleanup_mtls_test, run_curl_and_verify, setup_mtls_checker
from ngts.tests_nvos.general.security.test_ssh_cert_auth.helpers import (
    SshCertAuthHelper,
    get_random_key_type,
    get_random_principal,
    set_cert_auth,
    set_trusted_ca_key,
    verify_user_login,
)
from ngts.tests_nvos.interfaces.nvl_port.helpers import get_linked_ports_pair
from ngts.tests_nvos.interfaces.nvl_port.phy_recovery_helpers import (
    nvl7_stage_and_apply_leaves,
    nvl7_verify_new_leaves,
    nvl7_wait_trunk_up,
    select_nvl7_trunk_fae_port,
)
from ngts.tests_nvos.interfaces.nvl_port.nvl6.test_port_phy_role import (
    get_fae_objs,
    role_case,
    verify_phy_role_default
)
from ngts.tests_nvos.interfaces.nvl_port.test_link_low_power import (
    get_linked_ports_objs,
    low_power_state_case,
    should_skip_if_low_power_not_supported
)
from ngts.tests_nvos.helpers.interfaces import interface_helpers
from ngts.tests_nvos.helpers.interfaces.nvl_port.nvl6 import link_training_helpers
from ngts.tests_nvos.system.aaa.helpers import create_new_user
from ngts.tests_nvos.system.gnmi.helpers import verify_gnmi_client_tools_installed
from ngts.tests_nvos.system.test_system_api_compression import verify_api_compression_state
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_general_utils import (
    wait_for_ldap_nvued_restart_workaround,
)
from ngts.tools.test_utils.switch_recovery import generate_strong_password

from .helpers import Result, SystemPackage

logger = logging.getLogger(__name__)
CheckerFn = Callable[..., Generator[None, None, None]]


class Skipped(Exception):
    """
    Skipped checker.
    """

    def __init__(self, reason: str):
        self.reason = reason


def run_checkers(*, engines: EnginesT, devices: DevicesT, base: SystemPackage, target: SystemPackage) -> Generator[list[Result], None, list[Result]]:
    """
    Run the feature checkers.

    Args:
        engines: The engines.
        devices: The devices.
        min_ver: The minimal version of the nvos.

    Returns:
        A generator that yields a list of results.
    """
    errors, checkers = [], []
    errors: list[Result]
    min_ver = _get_min_version(base.nvos, target.nvos)
    max_ver = target.nvos.name if min_ver == base.nvos.name else base.nvos.name
    for checker in _CHECKERS:
        try:
            checker_name = checker.__name__
            if checker_name.startswith("_"):
                checker_name = checker_name[1:]

            with allure.step(f"Running pre-checker: {checker_name}"):
                f = checker(engines=engines, devices=devices, sys_min_ver=min_ver, sys_max_ver=max_ver)
                try:
                    next(f)
                    checkers.append((checker_name, f))
                except Skipped as e:
                    logger.info("Skipping checker %s - %s", checker_name, e.reason)
        except Exception as e:
            logger.error(e)
            errors.append(Result(ok=False, operation=checker_name, error_message=str(e)))

    yield errors

    for checker_name, checker in checkers:
        try:
            with allure.step(f"Running checker: {checker_name}"):
                try:
                    next(checker)
                    logger.info("Checker %s finished", checker_name)
                except StopIteration:
                    logger.info("Checker %s finished", checker_name)
        except Exception as e:
            logger.error(e)
            errors.append(Result(ok=False, operation=checker_name, error_message=str(e)))

    yield errors


def _parse_nvos_version(nvos: str) -> float:
    """
    Convert nvos version to int.
    Example: 25.02.2200 -> 22200

    - Remove the product ID (the prefix 25)
    - convert that patch to float
    - convert the minor to int and multiply by 10,000
    - add the patch and the minor
    - as a result 25.02.2423-02 -> 2,002,423.02
    """
    nvos = nvos.replace("nvos-", "").replace("amd64-", "")
    minor, patch = re.search(r".*?25\.(\d+)\.([^\.]+)", nvos).groups()  # 25.02.2200 -> 02, 2200
    ver = float(patch.replace("-", "."))  # 2200 -> 22.0
    ver += int(minor) * 1_000_000  # 25.02.2200 -> 2,002,200.0
    return ver


def _is_valid_version(checker_minimal_version: str | int, nvos_minimal_version: str | int) -> bool:
    """
    Check if the nvos version is greater or equal to the checker minimal version.

    Args:
        checker_minimal_version: The minimal version of the checker (that argument is supplied by the decorator).
        nvos_minimal_version: The minimal version of the nvos (the minimal version between the base and target nvos).

    Returns:
        True if the nvos version is greater or equal to the checker minimal version, False otherwise.
    """
    if isinstance(checker_minimal_version, str):
        checker_minimal_version = _parse_nvos_version(checker_minimal_version)
    if isinstance(nvos_minimal_version, str):
        nvos_minimal_version = _parse_nvos_version(nvos_minimal_version)

    return checker_minimal_version <= nvos_minimal_version


def _get_min_version(nvos_ver_1: Path | None, nvos_ver_2: Path | None) -> str | None:
    """
    Get the minimum version of two nvos versions.
    """
    if nvos_ver_1 is None:
        return nvos_ver_2.name
    if nvos_ver_2 is None:
        return nvos_ver_1.name

    if _parse_nvos_version(nvos_ver_1.name) <= _parse_nvos_version(nvos_ver_2.name):
        return nvos_ver_1.name
    return nvos_ver_2.name


def _requires_compatibility(*valid_devices: IbDevice, minimal_version: str | int = 0, maximal_version: str | int | None = None):
    """
    Decorator to check if the checker is compatible with the devices and the minimal version.

    Args:
        *valid_devices: The valid devices for the checker.
        minimal_version: The minimal version for the checker.
        maximal_version: The maximal version for the checker.
    """

    def decorator(func: CheckerFn) -> CheckerFn:
        """Wrapper to check if the checker is compatible with the devices and the minimal version."""

        @functools.wraps(func)
        def wrapper(engines: EnginesT, devices: DevicesT, sys_min_ver: str, sys_max_ver: str, *args, **kwargs):
            """Wrapper to check if the checker is compatible with the devices and the minimal version."""
            valid_min_version = _is_valid_version(minimal_version, sys_min_ver)
            valid_max_version = True
            if maximal_version is not None:
                # If maximal_version is set, sys_max_ver is the max nvos version and maximal_version
                # is the checker's max allowed version. Run checker only if checker's max > nvos version.
                # Examples:
                # checker_version(decorator maximal_version) "4100" nvos_version "2500" -> should run (4100 > 2500)
                # checker_version(decorator maximal_version) "4100" nvos_version "4300" -> should not run (4100 < 4300)
                valid_max_version = not _is_valid_version(maximal_version, sys_max_ver)
            valid_device = not valid_devices or isinstance(devices.dut, valid_devices)
            if valid_min_version and valid_device and valid_max_version:
                yield from func(*args, engines=engines, devices=devices, **kwargs)
            else:
                skip_reason = ""
                if not valid_device:
                    skip_reason += f"invalid DUT type {devices.dut} "
                if not valid_min_version:
                    skip_reason += f"minimal version {minimal_version} is not met "
                if not valid_max_version:
                    skip_reason += f"maximal version {maximal_version} is not met "
                raise Skipped(skip_reason)

        return wrapper

    return decorator


# ####################### Example Feature Checkers #######################


@_requires_compatibility(
    IbDevice.JulietAriel, IbDevice.JulietNonScaleoutSwitchGB300, minimal_version="25.02.2200", maximal_version="25.02.4200"
)
def _check_some_feature(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    # XXX Example:
    """
    Checker Requirements:
    - Checker must be decorated with @_requires_compatibility
    - _requires_compatibility should have the valid devices for the feature
    - _requires_compatibility should have the minimal version for the feature
    - _requires_compatibility may have the maximal version for the feature (optional)
    - Checker must have yield statement
    - first part is the setup
    - second part is the test
    - using assert to verify the test is highly recommended
    - add the checker to the _CHECKERS list
    """
    ...  # setup the config actions for the rbac
    yield
    ...  # test rbac functionality
    assert ..., "RBAC test failed ..."


# #################### End of Example Feature Checkers ###################

# ####################### Feature Checkers #######################


@_requires_compatibility(IbDevice.JulietSwitch, minimal_version="25.02.2100", maximal_version="25.02.4100")
def _check_nmx_cert(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    if devices.dut.has_nmx:
        with allure.step("setup"):
            tmp_certs_dir, nmx_certs, encryption_mode = (
                setup_cluster_app_mngr_security_checker(engines)
            )
            certs: dict[str, CertInfo] = {
                ClusterApps.NMX_CONTROLLER: nmx_certs[0],
                ClusterApps.NMX_TELEMETRY: nmx_certs[1],
            }

    yield  # upgrade to the target nvos

    try:
        if devices.dut.has_nmx:
            with allure.step("verify after upgrade"):
                for app_name in ClusterApps.ALL_APPS:
                    with allure.independent_step(app_name):
                        cert = certs[app_name]
                        with allure.independent_step("Verify values in show kept"):
                            verify_manager_show(
                                app_name,
                                expect_cert=cert.name,
                                expect_cacert=cert.cacert_name,
                                expect_encryption=encryption_mode,
                            )
                        with allure.independent_step(f"verify client connection: client mode: {encryption_mode}. expect success: True"):
                            run_manager_hello_request(
                                app_name,
                                encryption_mode,
                                cert,
                                cert,
                                cert,
                                cert,
                                skip_etc_mapping=True,
                            ).verify_result()
    finally:
        cleanup_certs_for_tests(tmp_certs_dir, nmx_certs)


@_requires_compatibility(minimal_version="25.02.2100", maximal_version="25.02.4100")
def _check_api_mtls_old(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    with allure.step("setup"):
        tmp_certs_dir, server_cert, server_ca = setup_mtls_checker(engines)
        feature_resource = System().api
        installed_app_name = API_INSTALLED
        verify_connection_func = run_curl_and_verify
        non_matching_client_cert_should_work = True

    yield  # upgrade to the target nvos

    try:
        with allure.step("verify mtls after this factory reset"):
            with allure.independent_step("verify mtls is configured in show"):
                verify_ca_configuration(feature_resource, installed_app_name, server_ca.cacert_name)
            with allure.independent_step("verify mtls only connection"):
                verify_connection(
                    TestFlowType.ALL_TYPES,
                    engines.dut,
                    UserInfo(engines.dut.username, engines.dut.password, "admin"),
                    True,
                    server_cert,
                    server_ca,
                    non_matching_client_cert_should_work,
                    verify_connection_func,
                )
    finally:
        cleanup_mtls_test(tmp_certs_dir, [server_cert], [server_ca])


@_requires_compatibility(minimal_version="25.02.2100")
def _check_cert_mgmt(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    security = System().security

    with allure.step("setup"):
        tmp_certs_dir, cert, cas = setup_cert_mgmt_checker(engines)

    yield  # upgrade to the target nvos

    try:
        with allure.step("verify ca/certificates kept"):
            with allure.independent_step("verify cert exist in show"):
                out = OutputParsingTool.parse_json_str_to_dictionary(security.certificate.show()).get_returned_value()
                assert cert.name in out, f"cert {cert.name} expected to be in output but is not\n{out}"
            with allure.independent_step("verify cas exist"):
                out = OutputParsingTool.parse_json_str_to_dictionary(security.ca_certificate.show()).get_returned_value()
                missing_cas = [ca.ca_info.name for ca in cas if ca.ca_info.name not in out]
                assert not missing_cas, f"{missing_cas} are missing from ca show output\n{out}"
            with allure.independent_step("verify cert in certs locations"):
                verify_cert_in_expected_locations(cert.name, engines.dut)
            with allure.independent_step("verify cas in expected locations"):
                for ca in cas:
                    with allure.independent_step(ca.ca_info.name):
                        verify_ca_in_expected_locations(ca.ca_info.name, ca.ca_info, engines.dut, ca.external)
    finally:
        with allure.step("cleanup"):
            cleanup_certs_for_tests(tmp_certs_dir, [cert], [ca.ca_info for ca in cas])


@_requires_compatibility(minimal_version="25.02.4200")
def _check_rbac(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    system = System()

    test_class_name = "TestClass"
    test_role_name = "TestRole"
    interface_path = "/interface/"

    with allure.step("Create RBAC class"):
        system.aaa.class_rbac.set_new_class(test_class_name, RbacConsts.ALLOW, interface_path, permission="all")

    with allure.step("Create RBAC role"):
        system.aaa.role.set_new_role(test_role_name, test_class_name, apply=True)

    with allure.step("Create user with new role"):
        test_user, test_password = create_new_user(role=test_role_name, apply=True)

    with allure.step("Save configuration"):
        NvueGeneralCli.save_config(engines.dut)

    yield  # upgrade to the target nvos

    with allure.step("Verify after upgrade"):
        with allure.step("Verify RBAC class in show"):
            class_output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.class_rbac.show()).get_returned_value()
            ValidationTool.verify_field_value_exist_in_output_dict(class_output, test_class_name).verify_result()

        with allure.step("Verify RBAC role in show"):
            role_output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.role.show()).get_returned_value()
            ValidationTool.verify_field_value_exist_in_output_dict(role_output, test_role_name).verify_result()

        verify_rbac_classes_in_role(system, test_role_name, [test_class_name])

        with allure.step("Verify user does exist or has default permissions"):
            user_output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.user.show()).get_returned_value()
            ValidationTool.verify_field_value_exist_in_output_dict(user_output, test_user).verify_result()


@_requires_compatibility(minimal_version="25.02.2100")
def _check_sed_password(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    tpm_tool = TpmTool(engines.dut)

    yield  # upgrade to the target nvos

    devices.dut.verify_sed_password(tpm_tool)


@_requires_compatibility(minimal_version="25.02.4200")
def _check_gnmi_mtls_spiffe_id_and_crl(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    scp_player = get_scp_player(engines)
    hostname = get_dut_hostname(engines)
    cert_name_prefix = "gnmi"
    verify_gnmi_client_tools_installed()

    with allure.step("setup"):
        with allure.step("prepare certs, users, spiffes"):
            setup: TestSetup = setup_test(hostname, engines, scp_player, cert_name_prefix=cert_name_prefix)
            client_cert = setup.cert_spif_of_user1_1
            server_cert = setup.server_cert

        with allure.step("prepare mtls"):
            crl_validator = GnmiCrlValidator(host=hostname, ip=engines.dut.ip)
            crl_validator.prepare_mtls(server_certs=[server_cert], client_cas=[client_cert])
            time.sleep(15)  # Wait for gnmi server to be ready

        with allure.step("verify spiffe works before upgrade"):
            check_spiffe_positive_gnmi(engines, setup)

        with allure.step("prepare crl"):
            crl_dest = os.path.dirname(os.path.dirname(setup.cert_spif_of_user1_1.public))
            crl_name = "test_crl_gnmi_upgrade"
            crl_path = crl_validator.revoke_cert(
                crl_name=crl_name,
                cert=setup.cert_spif_of_user1_1,
                config=RevokeConfig(dest=crl_dest, ca_dest=os.path.join(crl_dest, "ca")),
            )

        with allure.step("bind crl"):
            crl_validator.bind_crl(crl_path, crl_name)

        with allure.step("Make request via client application and see it fails"):
            crl_validator.run_client(
                ClientConfig(
                    user=setup.user1,
                    expect_success=False,
                    client_cert=setup.cert_spif_of_user1_1,
                    client_cacert=server_cert,
                )
            )

        with allure.step("save config"):
            NvueGeneralCli.save_config(engines.dut)

    yield  # upgrade to the target nvos

    with allure.step("Make request via client application and see it fails"):
        crl_validator.run_client(ClientConfig(user=setup.user1, expect_success=False, client_cert=client_cert, client_cacert=server_cert))

    crl_validator.unbind_crl()

    with allure.step("Make request via client application and see it succeeds"):
        crl_validator.run_client(ClientConfig(user=setup.user1, expect_success=True, client_cert=client_cert, client_cacert=server_cert))

    with allure.step("verify spiffe works after upgrade"):
        check_spiffe_positive_gnmi(engines, setup)


@_requires_compatibility(minimal_version="25.02.4200")
def _check_api_mtls_spiffe_id_and_crl(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    scp_player = get_scp_player(engines)
    hostname = get_dut_hostname(engines)

    with allure.step("setup"):
        with allure.step("prepare certs, users, spiffes"):
            setup: TestSetup = setup_test(hostname, engines, scp_player)
            client_cert = setup.cert_spif_of_user1_1
            server_cert = setup.server_cert

        with allure.step("prepare mtls"):
            crl_validator = ApiCrlValidator(host=hostname, ip=engines.dut.ip)
            crl_validator.prepare_mtls(server_certs=[server_cert], client_cas=[client_cert])

        with allure.step("verify spiffe works before upgrade"):
            with allure.step("take new revision number for testing admin permissions"):
                revision_num = get_tmp_revision_number_for_test_only(
                    CertInfo(
                        "",
                        "",
                        setup.cert_spif_of_user1_1.private,
                        setup.cert_spif_of_user1_1.public,
                        "",
                        "",
                        setup.cert_spif_of_user1_1.ip,
                        setup.cert_spif_of_user1_1.ip,
                        setup.server_cert.cacert,
                    )
                )
            check_spiffe_positive_api(engines, revision_num, setup)

        with allure.step("prepare crl"):
            crl_dest = os.path.dirname(os.path.dirname(setup.cert_spif_of_user1_1.public))
            crl_name = "test_crl_api_upgrade"
            crl_path = crl_validator.revoke_cert(
                crl_name=crl_name,
                cert=setup.cert_spif_of_user1_1,
                config=RevokeConfig(dest=crl_dest, ca_dest=os.path.join(crl_dest, "ca")),
            )

        with allure.step("bind crl"):
            crl_validator.bind_crl(crl_path, crl_name)

        with allure.step("Make request via client application and see it fails"):
            crl_validator.run_client(
                ClientConfig(
                    user=setup.user1,
                    expect_success=False,
                    client_cert=setup.cert_spif_of_user1_1,
                    client_cacert=server_cert,
                )
            )

        with allure.step("save config"):
            NvueGeneralCli.save_config(engines.dut)

    yield  # upgrade to the target nvos

    with allure.step("Make request via client application and see it fails"):
        crl_validator.run_client(ClientConfig(user=setup.user1, expect_success=False, client_cert=client_cert, client_cacert=server_cert))

    crl_validator.unbind_crl()

    with allure.step("verify spiffe works after upgrade"):
        with allure.step("take new revision number for testing admin permissions"):
            revision_num = get_tmp_revision_number_for_test_only(
                CertInfo(
                    "",
                    "",
                    setup.cert_spif_of_user1_1.private,
                    setup.cert_spif_of_user1_1.public,
                    "",
                    "",
                    setup.cert_spif_of_user1_1.ip,
                    setup.cert_spif_of_user1_1.ip,
                    setup.server_cert.cacert,
                )
            )
        check_spiffe_positive_api(engines, revision_num, setup)


@_requires_compatibility(IbDevice.JulietSwitch, minimal_version="25.02.4200")
def _check_nmx_controller_rbac(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify RBAC upgrade works as expected
    Test flow:
        1. Enable cluster
        2. Prepare rbac for nmx controller
        3. Run app client with good user - Should succeed
        4. Run app client with bad user - Should fail
        5. Perform upgrade
        6. Run app client with good user - Should succeed
        7. Run app client with bad user - Should fail
        8. Restore rbac file
        9. Run app client with bad user - Should succeed
    """
    cluster = Cluster()
    dut_hostname = engines.dut.ip
    scp_player = get_scp_player(engines)
    verify_gnmi_client_tools_installed()

    with allure.step("enable cluster"):
        enable_cluster()

    with allure.step("prepare rbac"):
        cluster_app_nmx_c: ClusterApp = cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER]
        rbac_tool_nmx_c = NmxRbacTool(cluster, engines.dut, cluster_app_nmx_c)
        rbac_file_name = "controller_rbac_upgrade"
        certs_location = get_test_certs_dir_location("controller_rbac_upgrade", dut_hostname)
        certs_location, certs = setup_certs_for_tests(
            certs_dirname_prefix=certs_location,
            certs_names=["client_nmx_c", "server_nmx_c"],
            engines=engines,
            dut_hostname=dut_hostname,
            scp_player=scp_player,
            dut_ip=engines.dut.ip,
            create_chain=False,
        )
        client_cert_nmx_c = certs[0]
        server_cert_nmx_c = certs[1]
        rbac_tool_nmx_c.prepare_nmx_certs([server_cert_nmx_c], [client_cert_nmx_c])

    rbac_file_path = RbacConsts.NMX_RBAC_FILE_USER_PATH
    try:
        rbac_tool_nmx_c.import_rbac_file(rbac_file_name, rbac_file_path)

        rbac_user = UserInfo("sasha", "sasha_rbac", "admin")
        bad_rbac_user = UserInfo("bad_user", "bad_password", "admin")

        rbac_tool_nmx_c.run_app_client(dut_hostname, rbac_user, client_cert_nmx_c, server_cert_nmx_c, expect_success=True)

        with allure.step("save config"):
            NvueGeneralCli.save_config(engines.dut)

        yield  # Do upgrade

        with allure.step("verify nmx controller mtls works after upgrade"):
            rbac_tool_nmx_c.run_app_client(dut_hostname, rbac_user, client_cert_nmx_c, server_cert_nmx_c, expect_success=True)
            rbac_tool_nmx_c.run_app_client(dut_hostname, bad_rbac_user, client_cert_nmx_c, server_cert_nmx_c, expect_success=True)

        with allure.step("update rbac file and mode"):
            rbac_tool_nmx_c.update_rbac_file(rbac_file_name)
            rbac_tool_nmx_c.update_rbac_mode(RbacConsts.RBAC_MODE_USERNAME_PASSWORD)

        with allure.step("verify nmx controller mtls works after applying rbac file and mode"):
            rbac_tool_nmx_c.run_app_client(dut_hostname, rbac_user, client_cert_nmx_c, server_cert_nmx_c, expect_success=True)
            rbac_tool_nmx_c.run_app_client(dut_hostname, bad_rbac_user, client_cert_nmx_c, server_cert_nmx_c, expect_success=False)

    finally:
        rbac_tool_nmx_c.restore_rbac_mode()
        rbac_tool_nmx_c.restore_rbac_file()


@_requires_compatibility(IbDevice.JulietSwitch, minimal_version="25.02.4200")
def _check_nmx_telemetry_rbac(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify RBAC upgrade works as expected
    Test flow:
        1. Enable cluster
        2. Prepare rbac for nmx telemetry
        3. Run app client with good user - Should succeed
        4. Run app client with bad user - Should fail
        5. Perform upgrade
        6. Run app client with good user - Should succeed
        7. Run app client with bad user - Should fail
        8. Restore rbac file
        9. Run app client with bad user - Should succeed
    """
    cluster = Cluster()
    dut_hostname = engines.dut.ip
    scp_player = get_scp_player(engines)
    verify_gnmi_client_tools_installed()

    with allure.step("enable cluster"):
        enable_cluster()

    with allure.step("prepare rbac"):
        cluster_app_nmx_t: ClusterApp = cluster.apps.app_name[ClusterConsts.NMX_TELEMETRY]
        rbac_tool_nmx_t = NmxRbacTool(cluster, engines.dut, cluster_app_nmx_t)
        rbac_file_name = "telemetry_rbac_upgrade"
        certs_location = get_test_certs_dir_location("telemetry_rbac_upgrade", dut_hostname)
        certs_location, certs = setup_certs_for_tests(
            certs_dirname_prefix=certs_location,
            certs_names=["client_nmx_t", "server_nmx_t"],
            engines=engines,
            dut_hostname=dut_hostname,
            scp_player=scp_player,
            dut_ip=engines.dut.ip,
            create_chain=False,
        )
        client_cert_nmx_t = certs[0]
        server_cert_nmx_t = certs[1]
        rbac_tool_nmx_t.prepare_nmx_certs([server_cert_nmx_t], [client_cert_nmx_t])

    rbac_file_path = RbacConsts.NMX_RBAC_FILE_USER_PATH
    try:
        rbac_tool_nmx_t.import_rbac_file(rbac_file_name, rbac_file_path)
        rbac_tool_nmx_t.update_rbac_file(rbac_file_name)
        rbac_tool_nmx_t.update_rbac_mode(RbacConsts.RBAC_MODE_USERNAME_PASSWORD)
        rbac_user = UserInfo("sasha", "sasha_rbac", "admin")
        bad_rbac_user = UserInfo("bad_user", "bad_password", "admin")

        rbac_tool_nmx_t.run_app_client(dut_hostname, rbac_user, client_cert_nmx_t, server_cert_nmx_t, expect_success=True)
        rbac_tool_nmx_t.run_app_client(dut_hostname, bad_rbac_user, client_cert_nmx_t, server_cert_nmx_t, expect_success=False)

        with allure.step("save config"):
            NvueGeneralCli.save_config(engines.dut)

        yield  # Do upgrade

        with allure.step("verify rbac works after upgrade"):
            rbac_tool_nmx_t.run_app_client(dut_hostname, rbac_user, client_cert_nmx_t, server_cert_nmx_t, expect_success=True)
            rbac_tool_nmx_t.run_app_client(dut_hostname, bad_rbac_user, client_cert_nmx_t, server_cert_nmx_t, expect_success=False)

        with allure.step("restore rbac mode and file"):
            rbac_tool_nmx_t.restore_rbac_mode()
            rbac_tool_nmx_t.restore_rbac_file()

        with allure.step("verify rbac works after restore"):
            rbac_tool_nmx_t.run_app_client(dut_hostname, rbac_user, client_cert_nmx_t, server_cert_nmx_t, expect_success=True)
            rbac_tool_nmx_t.run_app_client(dut_hostname, bad_rbac_user, client_cert_nmx_t, server_cert_nmx_t, expect_success=True)

    finally:
        rbac_tool_nmx_t.restore_rbac_mode()
        rbac_tool_nmx_t.restore_rbac_file()


@ _requires_compatibility(minimal_version="25.02.2100")
def _check_tacacs_auth(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify TACACS authentication upgrade works as expected.
    Test flow:
        1. Configure TACACS server
        2. Enable TACACS server
        3. Test TACACS auth through auth modes
        4. Save configuration
        5. Do upgrade
        6. Test TACACS auth through auth modes after upgrade
    """
    yield from _run_authentication_test(
        engines=engines,
        devices=devices,
        server_type="TACACS",
        server_config=TacacsDockerServer1.SERVER_BY_ADDRESSING_TYPE,
        auth_modes=AuthMode.ALL_TYPES,
        update_auth_mode_func=update_tacacs_server_auth_mode,
        aaa_obj=System().aaa.tacacs,
        remote_aaa_type=RemoteAaaType.TACACS,
    )


@ _requires_compatibility(minimal_version="25.02.2100")
def _check_radius_auth(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify RADIUS authentication upgrade works as expected.
    Test flow:
        1. Configure RADIUS server
        2. Enable RADIUS server
        3. Test RADIUS auth through auth modes
        4. Save configuration
        5. Do upgrade
        6. Test RADIUS auth through auth modes after upgrade
    """
    server_config = {
        AddressingType.IPV4: RadiusPhysicalServer.SERVER_IPV4,
        AddressingType.IPV6: RadiusVmServer.SERVER_IPV6,
        AddressingType.DN: RadiusVmServer.SERVER_DN,
    }
    yield from _run_authentication_test(
        engines=engines,
        devices=devices,
        server_type="RADIUS",
        server_config=server_config,
        auth_modes=RadiusConsts.AUTH_TYPES,
        update_auth_mode_func=update_radius_server_auth_type,
        aaa_obj=System().aaa.radius,
        remote_aaa_type=RemoteAaaType.RADIUS,
    )


@ _requires_compatibility(minimal_version="25.02.2100")
def _check_ldap_auth(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify LDAP authentication upgrade works as expected.
    Test flow:
        1. Configure LDAP server
        2. Enable LDAP server
        3. Test LDAP auth through auth modes
        4. Save configuration
        5. Do upgrade
        6. Test LDAP auth through auth modes after upgrade
    """
    yield from _run_authentication_test(
        engines=engines,
        devices=devices,
        server_type="LDAP",
        server_config=LdapServersP3.LDAP3_SERVERS,
        auth_modes=LdapEncryptionModes.ALL_MODES,
        update_auth_mode_func=update_ldap_encryption_mode,
        aaa_obj=System().aaa.ldap,
        remote_aaa_type=RemoteAaaType.LDAP,
        extra_setup_func=functools.partial(wait_for_ldap_nvued_restart_workaround, engine_to_use=engines.dut),
    )


@_requires_compatibility(IbDevice.RosalindSurrogateSwitch, minimal_version="25.03.0100")
def _check_nv_bridge_system_encryption(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify RBAC upgrade works as expected
    Test flow:
        1. Enable cluster
        2. Prepare system and cluster internal certs
        3. Configure system and cluster internal certs
        4. Save configuration
        5. Do upgrade
        6. Verify system and cluster internal certs are still configured after upgrade
        7. Perform cleanup
    """
    cluster = Cluster()
    system = System()
    dut_hostname = engines.dut.ip
    scp_player = get_scp_player(engines)
    verify_gnmi_client_tools_installed()

    with allure.step("enable cluster"):
        enable_cluster()

    with allure.step("prepare system and cluster internal certs"):
        certs_location = get_test_certs_dir_location("nv_bridge_upgrade", dut_hostname)
        certs_location, certs = setup_certs_for_tests(
            certs_dirname_prefix=certs_location,
            certs_names=["system_internal", "cluster_internal_nmx_c"],
            engines=engines,
            dut_hostname=dut_hostname,
            scp_player=scp_player,
            dut_ip=engines.dut.ip,
            create_chain=False,
        )
        system_internal_cert = certs[0]
        cluster_internal_nmx_c_cert = certs[1]

        with allure.step("import system and cluster internal certs"):
            import_certs_safely([system_internal_cert, cluster_internal_nmx_c_cert], scp_player)
            import_cas_safely([system_internal_cert, cluster_internal_nmx_c_cert], scp_player)

        nmx_c_app = cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER]

        with allure.step("Configure system"):
            system.internal.certificate.action_update(system_internal_cert.name).verify_result()
            system.internal.ca_certificate.action_update(system_internal_cert.cacert_name).verify_result()
            system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
            verify_system_internal_show(
                expect_cert=system_internal_cert.name, expect_cacert=system_internal_cert.cacert_name, expect_encryption=EncryptionMode.MTLS
            )
            verify_system_internal_cert_files(
                engines.dut, expected_cert_id=system_internal_cert.name, expected_cacert_id=system_internal_cert.cacert_name
            )

        with allure.step("Configure cluster nmx_c internal certs"):
            nmx_c_app.internal.certificate.action_update(cluster_internal_nmx_c_cert.name).verify_result()
            nmx_c_app.internal.ca_certificate.action_update(cluster_internal_nmx_c_cert.cacert_name).verify_result()
            nmx_c_app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
            wait_for_cluster_app_update(cluster, engines.dut)
            verify_cluster_app_internal_show(
                ClusterConsts.NMX_CONTROLLER,
                expect_cert=cluster_internal_nmx_c_cert.name,
                expect_cacert=cluster_internal_nmx_c_cert.cacert_name,
                expect_encryption=EncryptionMode.MTLS,
            )
            verify_cluster_app_internal_cert_files(
                ClusterConsts.NMX_CONTROLLER,
                engines.dut,
                expected_cert_id=cluster_internal_nmx_c_cert.name,
                expected_cacert_id=cluster_internal_nmx_c_cert.cacert_name,
            )

    try:
        with allure.step("save config"):
            NvueGeneralCli.save_config(engines.dut)

        yield  # Do upgrade

        with allure.step("verify system and cluster internal certs are still configured after upgrade"):
            verify_system_internal_show(
                expect_cert=system_internal_cert.name, expect_cacert=system_internal_cert.cacert_name, expect_encryption=EncryptionMode.MTLS
            )
            verify_system_internal_cert_files(
                engines.dut, expected_cert_id=system_internal_cert.name, expected_cacert_id=system_internal_cert.cacert_name
            )
            verify_cluster_app_internal_show(
                ClusterConsts.NMX_CONTROLLER,
                expect_cert=cluster_internal_nmx_c_cert.name,
                expect_cacert=cluster_internal_nmx_c_cert.cacert_name,
                expect_encryption=EncryptionMode.MTLS,
            )
            verify_cluster_app_internal_cert_files(
                ClusterConsts.NMX_CONTROLLER,
                engines.dut,
                expected_cert_id=cluster_internal_nmx_c_cert.name,
                expected_cacert_id=cluster_internal_nmx_c_cert.cacert_name,
            )

    finally:
        with allure.step("restore system and cluster internal certs"):
            system.internal.encryption.action_restore()
            system.internal.certificate.action_restore()
            system.internal.ca_certificate.action_restore()
            nmx_c_app.internal.encryption.action_restore()
            nmx_c_app.internal.certificate.action_restore()
            nmx_c_app.internal.ca_certificate.action_restore()


@_requires_compatibility(IbDevice.RosalindSurrogateSwitch, minimal_version="25.03.0600")
def _check_nv_bridge_internal_crl_spiffe(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify NV Bridge internal CRL + SPIFFE config survives upgrade.

    Test flow:
        1. Enable cluster
        2. Generate SPIFFE certs for system/cluster and a CRL revoking an unrelated cert
        3. Configure system internal: SPIFFE cert + CA + mTLS + CRL
        4. Configure cluster internal: SPIFFE cert + CA + mTLS + CRL
        5. Verify config pre-upgrade
        6. Save config and do upgrade
        7. Verify SPIFFE + CRL + cert config persisted after upgrade
    """
    cluster = Cluster()
    system = System()
    dut_hostname = engines.dut.ip
    scp_player = get_scp_player(engines)
    verify_gnmi_client_tools_installed()
    spiffe_uri = "spiffe://nvbridge/default"

    with allure.step("enable cluster"):
        enable_cluster()

    with allure.step("generate SPIFFE certificates for system and cluster"):
        certs_location = get_test_certs_dir_location("nv_bridge_crl_spiffe_upgrade", dut_hostname)
        spiffe_certs_dir = os.path.join(certs_location, "spiffe_certs")

        system_cert = build_internal_bridge_cert(
            cert_name="upgrade-spiffe-sys",
            cert_info="system SPIFFE cert for upgrade",
            dut_hostname=dut_hostname,
            dut_ip=engines.dut.ip,
            cert_cn="nv-bridge-upgrade-sys",
            spiffe_uri=spiffe_uri,
        )
        cluster_cert = build_internal_bridge_cert(
            cert_name="upgrade-spiffe-cluster",
            cert_info="cluster SPIFFE cert for upgrade",
            dut_hostname=dut_hostname,
            dut_ip=engines.dut.ip,
            cert_cn="nv-bridge-upgrade-cluster",
            spiffe_uri=spiffe_uri,
        )
        generate_certs(spiffe_certs_dir, [system_cert, cluster_cert])

    with allure.step("generate CRL revoking unrelated cert"):
        crl_certs_dir = os.path.join(certs_location, "crl_certs")
        revoked_cert = build_internal_bridge_cert(
            cert_name="upgrade-revoked",
            cert_info="cert to revoke for CRL generation",
            dut_hostname=dut_hostname,
            dut_ip=engines.dut.ip,
            cert_cn="nv-bridge-upgrade-revoked",
        )
        generate_certs(crl_certs_dir, [revoked_cert])

        crl_name = "upgrade-bridge-crl"
        cert_gen = CertificateGenerator()
        ca_dest = os.path.dirname(revoked_cert.cacert) if revoked_cert.cacert else ""
        ca_name = os.path.splitext(os.path.basename(revoked_cert.cacert))[0] if revoked_cert.cacert else "ca"
        crl_path = cert_gen.revoke_cert(
            crl_certs_dir, crl_name, revoked_cert.name,
            ca_dest=ca_dest, ca_name=ca_name,
        )

    with allure.step("import certificates and CRL"):
        import_internal_certs(engines, [system_cert, cluster_cert], [system_cert, cluster_cert])
        import_crl_safely(crl_name, crl_path, scp_player)

    nmx_c_app = cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER]

    with allure.step("configure system internal: SPIFFE cert + CA + mTLS + CRL"):
        system.internal.certificate.action_update(system_cert.name).verify_result()
        system.internal.ca_certificate.action_update(system_cert.cacert_name).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        system.internal.crl.action_update(crl_name).verify_result()

        verify_system_internal_show(
            expect_cert=system_cert.name,
            expect_cacert=system_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
        )
        verify_system_internal_crl_show(expect_crl=crl_name)

    with allure.step("configure cluster nmx_c internal: SPIFFE cert + CA + mTLS + CRL"):
        nmx_c_app.internal.certificate.action_update(cluster_cert.name).verify_result()
        nmx_c_app.internal.ca_certificate.action_update(cluster_cert.cacert_name).verify_result()
        nmx_c_app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        wait_for_cluster_app_update(cluster, engines.dut)
        nmx_c_app.internal.crl.action_update(crl_name).verify_result()

        verify_cluster_app_internal_show(
            ClusterConsts.NMX_CONTROLLER,
            expect_cert=cluster_cert.name,
            expect_cacert=cluster_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
        )
        verify_cluster_app_internal_crl_show(ClusterConsts.NMX_CONTROLLER, expect_crl=crl_name)

    try:
        with allure.step("save config"):
            NvueGeneralCli.save_config(engines.dut)

        yield  # Do upgrade

        with allure.step("verify SPIFFE + CRL + cert config persisted after upgrade"):
            verify_system_internal_show(
                expect_cert=system_cert.name,
                expect_cacert=system_cert.cacert_name,
                expect_encryption=EncryptionMode.MTLS,
            )
            verify_system_internal_crl_show(expect_crl=crl_name)
            verify_system_internal_spiffe_json(engines.dut, expected_cert_spiffe=spiffe_uri)

            verify_cluster_app_internal_show(
                ClusterConsts.NMX_CONTROLLER,
                expect_cert=cluster_cert.name,
                expect_cacert=cluster_cert.cacert_name,
                expect_encryption=EncryptionMode.MTLS,
            )
            verify_cluster_app_internal_crl_show(ClusterConsts.NMX_CONTROLLER, expect_crl=crl_name)
            verify_cluster_app_internal_spiffe_json(
                engines.dut,
                ClusterConsts.NMX_CONTROLLER,
                expected_cert_spiffe=spiffe_uri,
            )

    finally:
        with allure.step("restore system and cluster internal config"):
            system.internal.crl.action_restore()
            system.internal.encryption.action_restore()
            system.internal.certificate.action_restore()
            system.internal.ca_certificate.action_restore()
            nmx_c_app.internal.crl.action_restore()
            nmx_c_app.internal.encryption.action_restore()
            nmx_c_app.internal.certificate.action_restore()
            nmx_c_app.internal.ca_certificate.action_restore()


@_requires_compatibility(minimal_version="25.02.6000")
def _check_ssh_cert_auth(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify SSH certificate authentication upgrade works as expected.
    Test flow:
        1. Generate SSH key pair and CA key pair
        2. Sign user certificate with CA
        3. Set principal and enable cert-auth for admin user
        4. Set trusted CA key on the system
        5. Verify user can login with certificate
        6. Save configuration
        7. Do upgrade
        8. Verify user can still login with certificate after upgrade
    """
    system = System()
    ssh_cert_auth_helper = SshCertAuthHelper()
    key_name = "upgrade_cert_test_key"
    key_type = get_random_key_type()
    principal = get_random_principal()
    admin_user = UserInfo(SystemConsts.DEFAULT_USER_ADMIN, SystemConsts.DEFAULT_USER_ADMIN, UserRole.ADMIN)
    hostname = engines.dut.ip

    try:
        with allure.step("Setup SSH certificate authentication"):
            ssh_cert_auth_helper.ensure_keys_directory()

            with allure.step("Generate keys and sign certificate"):
                ca_val, key_private_path = ssh_cert_auth_helper.generate_keys_and_sign_certificate(
                    key_name=key_name, key_type=key_type, principals=[principal]
                )

            with allure.step(f"Set cert-auth principal {principal} for admin"):
                set_cert_auth(system=system, user=admin_user, principal=principal, state="enabled", apply=False)

            with allure.step(f"Set trusted CA key {key_name}"):
                set_trusted_ca_key(system, key_name, key_type, ca_val, apply=True)

            with allure.step("Verify login with certificate before upgrade"):
                verify_user_login(admin_user, key_private_path, hostname, engines, expect_success=True)

            with allure.step("Save configuration"):
                NvueGeneralCli.save_config(engines.dut)

        yield  # Do upgrade

        with allure.step("Verify SSH cert auth after upgrade"):
            with allure.step("Verify login with certificate after upgrade"):
                verify_user_login(admin_user, key_private_path, hostname, engines, expect_success=True)

    finally:
        with allure.step("Cleanup SSH cert auth configuration"):
            try:
                system.ssh_server.trusted_ca_keys.unset()
                system.aaa.user.user_id[admin_user.username].ssh.cert_auth.unset(apply=True)
            except Exception as cleanup_err:
                logger.warning("Cleanup failed: %s", cleanup_err)
            ssh_cert_auth_helper.cleanup_generated_keys(key_name)


@_requires_compatibility(minimal_version="25.02.4500")
def _check_api_compression(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify API compression upgrade works as expected.
    Test flow:
        1. Set API compression to gzip
        2. Verify API compression is set to gzip
        3. Save configuration
        4. Do upgrade
        5. Verify API compression is set to gzip after upgrade
    """
    system = System()
    with allure.step('Set API compression to gzip'):
        system.api.set(op_param_name=SystemConsts.ApiConsts.COMPRESSION, op_param_value=SystemConsts.ApiConsts.CompressionT.GZIP, apply=True).verify_result()
        verify_api_compression_state(system, SystemConsts.ApiConsts.CompressionT.GZIP)
    with allure.step('Save configuration'):
        NvueGeneralCli.save_config(engines.dut)

    yield  # Do upgrade

    try:
        with allure.step('Verify API compression is set to gzip after upgrade'):
            verify_api_compression_state(system, SystemConsts.ApiConsts.CompressionT.GZIP)
    finally:
        with allure.step('Unset API compression'):
            system.api.unset(op_param=SystemConsts.ApiConsts.COMPRESSION, apply=True).verify_result()
            verify_api_compression_state(system, None)


@_requires_compatibility(IbDevice.RosalindSwitch, minimal_version="25.03.0300")
def _check_phy_role(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify phy role functionality on NVL ports.
    This test verifies the phy role feature on NVL devices, which allows a port to be configured to
    a specific role (primary, secondary, or auto). The test validates that the port's role is set correctly
    and remains stable across port state flips.
    Test Steps:
        1. Set ports phy-role to primary and secondary
        2. Verify ports phy-role after upgrade
    """
    with allure.step("Get linked ports pair"):
        linked_ports_pair: list[str] = list(get_linked_ports_pair(devices, engines))
        logger.info(f"Linked ports pair: {linked_ports_pair[0]} <-> {linked_ports_pair[1]}")
        fae_port_1, fae_port_2 = get_fae_objs(linked_ports_pair)

    try:
        with allure.step("Verify default phy-role on both ports"):
            verify_phy_role_default(fae_port_1, fae_port_2)
        with allure.step("Set ports phy-role to primary and secondary"):
            role_case(
                roles_to_set=[
                    (fae_port_1, PhyRoleConsts.PHY_ROLE, PhyRoleConsts.PhyRole.PRIMARY.value),
                    (fae_port_2, PhyRoleConsts.PHY_ROLE, PhyRoleConsts.PhyRole.SECONDARY.value)
                ],
                expected_values=[
                    (fae_port_1, PhyRoleConsts.PHY_ROLE, PhyRoleConsts.PhyRole.PRIMARY.value, PhyRoleConsts.PhyRole.PRIMARY.value),
                    (fae_port_2, PhyRoleConsts.PHY_ROLE, PhyRoleConsts.PhyRole.SECONDARY.value, PhyRoleConsts.PhyRole.SECONDARY.value)
                ]
            )

        with allure.step("Save configuration"):
            NvueGeneralCli.save_config(engines.dut)

        yield  # Do upgrade

        with allure.step("Verify ports phy-role after upgrade"):
            role_case(
                expected_values=[
                    (fae_port_1, PhyRoleConsts.PHY_ROLE, PhyRoleConsts.PhyRole.PRIMARY.value, [PhyRoleConsts.PhyRole.PRIMARY.value, PhyRoleConsts.PhyRole.FW_DEFAULT.value]),
                    (fae_port_2, PhyRoleConsts.PHY_ROLE, PhyRoleConsts.PhyRole.SECONDARY.value, [PhyRoleConsts.PhyRole.SECONDARY.value, PhyRoleConsts.PhyRole.FW_DEFAULT.value])
                ]
            )
    finally:
        with allure.step("Cleanup phy-role configuration"):
            fae_port_1.interface.link.unset(op_param_name=PhyRoleConsts.PHY_ROLE, apply=True, ask_for_confirmation=True).verify_result()
            fae_port_2.interface.link.unset(op_param_name=PhyRoleConsts.PHY_ROLE, apply=True, ask_for_confirmation=True).verify_result()
            fae_port_1.port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP).verify_result()
            fae_port_2.port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP).verify_result()


@_requires_compatibility(IbDevice.RosalindSwitch, minimal_version="25.03.0300")
def _check_low_power(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify low power functionality on NVL ports.
    This test verifies the low power feature on NVL devices, which allows a port to be configured to
    a specific low power state (enabled or disabled). The test validates that the port's low power state is set correctly
    and remains stable across port state flips.
    Test Steps:
        1. Verify default low power states on both ports
        2. Verify low power states on both ports after setting to enabled
        3. Save configuration
        4. Do upgrade
        5. Verify low power states after upgrade
        6. Cleanup low power configuration
    """
    if should_skip_if_low_power_not_supported():
        Skipped("Low power is not supported")

    with allure.step("Get linked ports pair"):
        linked_ports_pair: list[str] = list(get_linked_ports_pair(devices, engines))
        logger.info(f"Linked ports pair: {linked_ports_pair[0]} <-> {linked_ports_pair[1]}")
        linked_ports_objs = get_linked_ports_objs(devices, linked_ports_pair)

    try:
        with allure.step("Verify default low power states on both ports"):
            low_power_state_case(
                expected_values=[((port_obj, NvosConst.DISABLED, NvosConst.DISABLED) for port_obj in linked_ports_objs)]
            )
        with allure.step("Verify low power states on both ports after setting to enabled"):
            random_state = random.choice([NvosConst.ENABLED, NvosConst.DISABLED])
            low_power_state_case(
                ports_to_set=[((port_obj, random_state) for port_obj in linked_ports_objs)],
                expected_values=[((port_obj, random_state, random_state) for port_obj in linked_ports_objs)]
            )
        with allure.step("Save configuration"):
            NvueGeneralCli.save_config(engines.dut)

        yield  # Do upgrade

        with allure.step("Verify low power states after upgrade"):
            low_power_state_case(
                expected_values=[((port_obj, random_state, random_state) for port_obj in linked_ports_objs)]
            )

    finally:
        with allure.step("Cleanup low power configuration"):
            for port_obj in linked_ports_objs:
                port_obj.interface.link.low_power.unset(apply=True).verify_result()
            for port_obj in linked_ports_objs:
                port_obj.port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP).verify_result()


@_requires_compatibility(IbDevice.RosalindSwitch, minimal_version="25.03.0500")
def _check_link_training(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify link training params on NVL ports survive upgrade.
    Test Steps:
        1. Set fec-measure-mode to enabled on both ports
        2. Set fec-measure-fail-action to a random non-default value on both ports
        3. Verify both params on both ports
        4. Save configuration
        5. Do upgrade
        6. Verify both params are still set after upgrade
        7. Cleanup link-training configuration
    """
    with allure.step("Get linked ports pair"):
        linked_ports_pair: list[str] = list(get_linked_ports_pair(devices, engines))
        logger.info(f"Linked ports pair: {linked_ports_pair[0]} <-> {linked_ports_pair[1]}")
        fae_port_1, fae_port_2 = get_fae_objs(linked_ports_pair)
        fae_objs_tuple = (fae_port_1, fae_port_2)

    try:
        with allure.step("Set fec-measure-mode to enabled on both ports"):
            for fae in fae_objs_tuple:
                fae.interface.link.link_training.set(
                    op_param_name=consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE,
                    op_param_value=consts_nv.LinkTrainingConsts.FecMeasureMode.ENABLED.value,
                    apply=True,
                    ask_for_confirmation=True,
                ).verify_result()
            interface_helpers.wait_and_verify_link(
                [Port.Port(fae.port.name) for fae in fae_objs_tuple],
                timeout=ib_consts.InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED,
            )

        random_fail_action = random.choice(consts_nv.LinkTrainingConsts.FecMeasureFailAction.operational())
        with allure.step(f"Set fec-measure-fail-action to '{random_fail_action}' on both ports"):
            for fae in fae_objs_tuple:
                fae.interface.link.link_training.set(
                    op_param_name=consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION,
                    op_param_value=random_fail_action,
                    apply=True,
                    ask_for_confirmation=True,
                ).verify_result()
            interface_helpers.wait_and_verify_link(
                [Port.Port(fae.port.name) for fae in fae_objs_tuple],
                timeout=ib_consts.InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED,
            )

        expected_after_set: OperationalAppliedT = {
            consts_nv.ConfState.OPERATIONAL: {
                consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE: consts_nv.LinkTrainingConsts.FecMeasureMode.ENABLED.value,
                consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION: random_fail_action,
            },
            consts_nv.ConfState.APPLIED: {
                consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE: consts_nv.LinkTrainingConsts.FecMeasureMode.ENABLED.value,
                consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION: random_fail_action,
            },
        }
        with allure.step("Verify link-training params"):
            for fae in (fae_port_1, fae_port_2):
                with allure.step(f"Verify port {fae.port.name}"):
                    ValidationTool.compare_nested_dictionary_content(
                        fae.interface.link.link_training.parse_show_operational_applied(),
                        expected_after_set,
                    ).verify_result()

        with allure.step("Save configuration"):
            NvueGeneralCli.save_config(engines.dut)

        yield  # Do upgrade

        with allure.step("Verify link-training params after upgrade"):
            for fae in (fae_port_1, fae_port_2):
                with allure.step(f"Verify port {fae.port.name}"):
                    ValidationTool.compare_nested_dictionary_content(
                        fae.interface.link.link_training.parse_show_operational_applied(),
                        expected_after_set,
                    ).verify_result()

    finally:
        link_training_helpers.cleanup_link_training(devices, fae_objs_tuple)


@_requires_compatibility(IbDevice.PortiaCpoSwitch, minimal_version="25.03.2000")
def _check_phy_recovery_nvl7(engines: EnginesT, devices: DevicesT, **kwargs) -> Generator[None, None, None]:
    """
    Verify NVL7 CPO trunk-port phy-recovery config survives upgrade.

    Test Steps:
        1. Select an NVL7 trunk (sw*) port
        2. Set recovery-policy-config=explicit-config-controls + a numeric leaf (recovery-tx-toggle-time)
        3. Save configuration
        4. Do upgrade
        5. Verify both values are preserved after upgrade
        6. Cleanup phy-recovery configuration

    Feature lands in release 25.03.2000 (dev 2026-07-17).
    """
    # Feature is supported only on CPO-capable NVL7 devices (pruned on non-CPO).
    if not is_cpo_capable(devices.dut):
        raise Skipped("Requires a CPO-capable device")

    # A distinctive non-default value pair (policy enum + non-zero timer) so the
    # "preserved after upgrade" check is meaningful. Show echoes configured values literally.
    expected = {
        ib_consts.PhyRecoveryConsts.RECOVERY_POLICY_CONFIG:
            ib_consts.PhyRecoveryConsts.RecoveryPolicyConfig.EXPLICIT_CONFIG_CONTROLS,
        ib_consts.PhyRecoveryConsts.RECOVERY_TX_TOGGLE_TIME: 100,
    }

    with allure.step("Select an NVL7 trunk (sw*) port"):
        fae_port = select_nvl7_trunk_fae_port()
        if fae_port is None:
            raise Skipped("No NVL7 trunk (sw*) port available")

    try:
        with allure.step(f"Set NVL7 phy-recovery leaves {expected}"):
            nvl7_stage_and_apply_leaves(fae_port, expected.items())
            # Bring the trunk back up right away (the apply admin-DOWNs it and it stays down)
            # so other feature-checkers' setups don't run with a degraded sw* link.
            nvl7_wait_trunk_up(fae_port.port.name)

        with allure.step("Save configuration"):
            NvueGeneralCli.save_config(engines.dut)

        yield  # Do upgrade

        with allure.step("Verify phy-recovery config preserved after upgrade"):
            nvl7_verify_new_leaves(fae_port, expected)
    finally:
        with allure.step("Cleanup NVL7 phy-recovery configuration and restore trunk link"):
            fae_port.port.interface.link.phy_recovery.unset(
                apply=True, ask_for_confirmation=True).verify_result()
            try:
                nvl7_wait_trunk_up(fae_port.port.name)
            except Exception as err:
                logger.warning(f"NVL7 cleanup: trunk {fae_port.port.name} did not come back up: {err}")


# #################### End of Feature Checkers ###################


def _run_authentication_test(  # noqa: PLR0913
    engines: EnginesT,
    devices: DevicesT,
    server_type: str,
    server_config: dict,
    auth_modes: list[str],
    update_auth_mode_func,
    aaa_obj,
    remote_aaa_type: str,
    extra_setup_func=None,
    extra_cleanup_func=None,
    **kwargs,
) -> Generator[None, None, None]:
    """
    Generic authentication test function for TACACS, RADIUS and LDAP.

    Args:
        engines: The engines object
        devices: The devices object
        server_type: Type of authentication server (TACACS/RADIUS/LDAP)
        server_config: Server configuration dictionary
        auth_modes: List of authentication modes to test
        update_auth_mode_func: Function to update authentication mode
        aaa_obj: AAA object (tacacs_obj, radius_obj, ldap_obj)
        remote_aaa_type: Remote AAA type string
        extra_setup_func: Optional extra setup function to call
        extra_cleanup_func: Optional extra cleanup function to call
    """
    skip_auth_mediums = [AuthMedium.OPENAPI, AuthMedium.SCP]  # skip openapi and scp, as we have certificates configured
    test_flow = TestFlowType.GOOD_FLOW
    addressing_type = AddressingType.IPV4
    topology_obj = TestToolkit.topology_obj

    adminuser = _prepare_local_admin_user(engines, devices)
    server_active_conf = SimpleNamespace()

    try:
        with allure.step(f"Configure {remote_aaa_type} server"):
            server = server_config[addressing_type].copy()
            assert getattr(server, "users_per_auth_medium", None) is not None, (
                f'given server must have "users_per_auth_medium" attr\nserver: {server.hostname} - {server.port} - {server.docker_name}'
            )
            server_resource = aaa_obj.server.server_id[server.hostname]
            server.configure(engines)

        with allure.step(f"Enable {remote_aaa_type}"):
            aaa_obj.enable(apply=True, verify_res=False)
            update_active_aaa_server(server_active_conf, server)

            if extra_setup_func:
                extra_setup_func(server_active_conf)

        with allure.step(f"test {server_type.lower()} auth works before upgrade through auth modes: {auth_modes}"):
            for auth_mode in auth_modes:
                with allure.step(auth_mode):
                    with allure.step(f"Update test param: {auth_mode}"):
                        update_auth_mode_func(engines, server_active_conf, server, server_resource, auth_mode)
                        if extra_setup_func:
                            extra_setup_func(server_active_conf)
                    with allure.step("Test auth"):
                        verify_auth_mediums(
                            test_flow,
                            engines,
                            topology_obj,
                            True,
                            False,
                            server,
                            UserRole.ALL_ROLES,
                            [adminuser],
                            skip_auth_mediums=skip_auth_mediums,
                        )

        with allure.step("Save configuration"):
            NvueGeneralCli.save_config(engines.dut)

        yield  # Do upgrade

        with allure.step(f"test {server_type.lower()} auth through auth modes: {auth_modes} after upgrade"):
            for auth_mode in auth_modes:
                with allure.step(auth_mode):
                    with allure.step(f"Update test param: {auth_mode}"):
                        update_auth_mode_func(engines, server_active_conf, server, server_resource, auth_mode)
                    with allure.step("Test auth"):
                        verify_auth_mediums(
                            test_flow,
                            engines,
                            topology_obj,
                            True,
                            False,
                            server,
                            UserRole.ALL_ROLES,
                            [adminuser],
                            skip_auth_mediums=skip_auth_mediums,
                        )
    except Exception as e:
        logger.error("Authentication test failed for %s: %s", server_type, e)
        raise
    finally:
        if extra_cleanup_func:
            try:
                extra_cleanup_func(server_active_conf)
            except Exception as cleanup_error:
                logger.warning("Cleanup failed: %s", cleanup_error)


def _prepare_local_admin_user(engines, devices) -> UserInfo:
    """
    Prepare a local admin user for authentication testing.

    Args:
        engines: The engines object
        devices: The devices object

    Returns:
        UserInfo: The created admin user

    Raises:
        RuntimeError: If user creation fails
    """
    try:
        adminrole = devices.dut.aaa_admin_role
        adminuser = UserInfo(
            username=AaaConsts.LOCALADMIN,
            password=generate_strong_password(),
            role=adminrole,
        )
        set_local_users(engines, [adminuser], apply=True)
        return adminuser
    except Exception as e:
        logger.error("Failed to create local admin user: %s", e)
        raise RuntimeError(f"User creation failed: {e}") from e


@_requires_compatibility(minimal_version="25.02.2100")
def _check_speed_configuration(
    engines: EnginesT, devices: DevicesT, base: SystemPackage, target: SystemPackage, **kwargs
) -> Generator[None, None, None]:
    """
    Verify that interface speed configuration is preserved across upgrade.

    Test flow:
        1. Configure speed on a random port (before upgrade)
        2. Perform upgrade
        3. Verify speed configuration preserved after upgrade
        4. Cleanup speed testing configuration

    Note: This replaces the speed testing from the deprecated test_downgrade_upgrade
    in test_system_image.py
    """
    speed_info = None

    with allure.step("Configure and test interface speeds"):
        try:
            speed_info = InterfaceConfigurationTool.choose_random_port_and_test_speed_configuration(engines, devices)

            if speed_info:
                logger.info("Speed testing configured successfully on port: %s", speed_info[0].name)
        except Exception as e:
            logger.warning("Speed testing setup failed, will skip this checker: %s", e)
            raise Skipped(f"Speed testing setup failed: {e}") from e

    yield  # Perform upgrade

    # Verify and cleanup after upgrade
    with allure.step("Verify speed configuration preserved and cleanup"):
        if speed_info:
            NvosInstallationSteps.cleanup_speed_testing_if_performed(speed_info, devices.dut)


@ _requires_compatibility(minimal_version="25.02.2351")
def _check_ssd_firmware_auto_upgrade(
    engines: EnginesT, devices: DevicesT, **kwargs
) -> Generator[None, None, None]:
    """
    Verify SSD auto-upgrade works correctly during NVOS upgrade.

    Test Steps:
    1. Verify device is on latest version.
    2. Downgrade SSD firmware to previous version
    3. yield (NVOS upgrade happens which includes latest SSD firmware)
    4. Verify SSD firmware is at latest version after upgrade

    """
    ssd_component = Platform().firmware.ssd

    _, _, latest_version_name = FWComponentsTool.get_fw_component_version_latest(FW_COMPONENT_SSD)
    if latest_version_name is None:
        raise Skipped("SSD firmware checker skipped: non supported SSD model")

    # Step 1: Verify device is on latest SSD version
    with allure.step('Verify device is on latest SSD version'):
        FWComponentsTool.verify_platform_component_version(ssd_component, latest_version_name)

    # Downgrade SSD firmware to previous version
    _, previous_filename, previous_version_name = FWComponentsTool.get_fw_component_version_previous(FW_COMPONENT_SSD)
    SSDTool.downgrade_ssd_firmware(engines, ssd_component, previous_filename, previous_version_name)

    yield  # NVOS upgrade happens here (includes latest SSD firmware in bundle)

    with allure.step("Verify SSD firmware after upgrade"):
        FWComponentsTool.verify_platform_component_version(ssd_component, latest_version_name)


# the checker must be called e.g. test_rbac
_CHECKERS: list[CheckerFn] = [
    _check_ssd_firmware_auto_upgrade,
    _check_nmx_cert,
    _check_api_mtls_old,
    _check_cert_mgmt,
    _check_rbac,
    _check_sed_password,
    _check_gnmi_mtls_spiffe_id_and_crl,
    _check_api_mtls_spiffe_id_and_crl,
    _check_nmx_controller_rbac,
    _check_nmx_telemetry_rbac,
    _check_nv_bridge_system_encryption,
    _check_nv_bridge_internal_crl_spiffe,
    _check_speed_configuration,
    _check_ssh_cert_auth,
    _check_api_compression,
    _check_phy_role,
    _check_link_training,
    _check_phy_recovery_nvl7,
]

_CHECKERS.append(
    random.choice([_check_tacacs_auth, _check_radius_auth, _check_ldap_auth])
)  # This is intended to be random, as two AAA checkers can't run together
