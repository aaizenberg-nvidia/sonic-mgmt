"""
NMX RBAC factory reset checkers for testing RBAC configuration persistence across factory reset operations.

These checkers verify that NMX Controller and Telemetry RBAC configurations are properly
reset after a factory reset with no parameters.
"""

from __future__ import annotations

import logging
from collections.abc import Generator

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import ClusterConsts, RbacConsts
from ngts.nvos_tools.infra.NmxRbacTool import NmxRbacTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.nmx.Apps import ClusterApp
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.helpers import cleanup_certs_for_tests, get_test_certs_dir_location, setup_certs_for_tests
from ngts.tests_nvos.general.security.nmx_cert.helpers import enable_cluster
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tests_nvos.system.gnmi.helpers import get_scp_player, verify_gnmi_client_tools_installed
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


def _setup_nmx_rbac(app_name: str, certs_prefix: str):
    """Setup NMX RBAC for a specific app. Returns tuple of (rbac_tool, certs_location, certs, users)."""
    engines = TestToolkit.engines
    cluster = Cluster()
    dut_hostname = engines.dut.ip
    scp_player = get_scp_player(engines)
    verify_gnmi_client_tools_installed()

    enable_cluster()

    cluster_app: ClusterApp = cluster.apps.app_name[app_name]
    rbac_tool = NmxRbacTool(cluster, engines.dut, cluster_app)
    rbac_file_name = f"{certs_prefix}_rbac"
    certs_location = get_test_certs_dir_location(certs_prefix, dut_hostname)
    certs_location, certs = setup_certs_for_tests(
        certs_dirname_prefix=certs_location,
        certs_names=[f"client_{certs_prefix}", f"server_{certs_prefix}"],
        engines=engines,
        dut_hostname=dut_hostname,
        scp_player=scp_player,
        dut_ip=engines.dut.ip,
        create_chain=False,
    )
    client_cert = certs[0]
    server_cert = certs[1]
    rbac_tool.prepare_nmx_certs([server_cert], [client_cert])

    rbac_user = UserInfo("sasha", "sasha_rbac", "admin")
    bad_rbac_user = UserInfo("bad_user", "bad_password", "admin")

    return rbac_tool, cluster_app, rbac_file_name, certs_location, certs, client_cert, server_cert, rbac_user, bad_rbac_user, dut_hostname


def _cleanup_nmx_rbac_certs(rbac_tool: NmxRbacTool, certs_location: str, certs: list[CertInfo]) -> None:
    """
    Release the certs from the app manager, then delete them.

    The post-reset verification re-binds the certs to run its clients, so at teardown they are still in use
    and deleting them would be rejected with "is currently being used by the following applications".
    """
    rbac_tool.unbind_nmx_certs()
    cleanup_certs_for_tests(certs_location, certs)


def _verify_rbac_reset(setup_context: tuple):  # noqa: PLR0913
    """Verify RBAC is reset after factory reset."""
    rbac_tool, cluster_app, _, _, _, client_cert, server_cert, rbac_user, bad_rbac_user, dut_hostname = setup_context
    enable_cluster()

    with allure.step("verify RBAC mode is disabled after factory reset"):
        rbac_mode_output = cluster_app.rbac.mode.show()
        assert "disabled" in rbac_mode_output, f"RBAC mode should be disabled: {rbac_mode_output}"

    with allure.step("verify RBAC file is cleared after factory reset"):
        rbac_file_output = cluster_app.rbac.file.parse_show()[ClusterConsts.NMX_RBAC_FILE]
        assert not rbac_file_output, f"RBAC file should be cleared: {rbac_file_output}"

    with allure.step("re-setup certs for verification"):
        rbac_tool.prepare_nmx_certs([server_cert], [client_cert])

    with allure.step("verify both users can connect (RBAC disabled)"):
        rbac_tool.run_app_client(dut_hostname, rbac_user, client_cert, server_cert, expect_success=True)
        rbac_tool.run_app_client(dut_hostname, bad_rbac_user, client_cert, server_cert, expect_success=True)


def nmx_controller_rbac_factory_reset_no_params_check() -> Generator[None, None, None]:
    """Verify NMX Controller RBAC configuration is cleared after factory reset with no params."""
    engines = TestToolkit.engines
    devices = TestToolkit.devices

    if not devices.dut.has_nmx:
        yield
        yield
        return

    setup = _setup_nmx_rbac(ClusterConsts.NMX_CONTROLLER, "controller_rbac_fr")
    rbac_tool, cluster_app, rbac_file_name, certs_location, certs, client_cert, server_cert, rbac_user, bad_rbac_user, dut_hostname = setup

    try:
        rbac_tool.import_rbac_file(rbac_file_name, RbacConsts.NMX_RBAC_FILE_USER_PATH)
        rbac_tool.update_rbac_file(rbac_file_name)
        rbac_tool.update_rbac_mode(RbacConsts.RBAC_MODE_USERNAME_PASSWORD)

        with allure.step("verify RBAC works before factory reset"):
            rbac_tool.run_app_client(dut_hostname, rbac_user, client_cert, server_cert, expect_success=True)
            rbac_tool.run_app_client(dut_hostname, bad_rbac_user, client_cert, server_cert, expect_success=False)

        NvueGeneralCli.save_config(engines.dut)

        yield  # factory reset

        _verify_rbac_reset(setup)

    finally:
        _cleanup_nmx_rbac_certs(rbac_tool, certs_location, certs)

    yield


def nmx_telemetry_rbac_factory_reset_no_params_check() -> Generator[None, None, None]:
    """Verify NMX Telemetry RBAC configuration is cleared after factory reset with no params."""
    engines = TestToolkit.engines
    devices = TestToolkit.devices

    if not devices.dut.has_nmx:
        yield
        yield
        return

    setup = _setup_nmx_rbac(ClusterConsts.NMX_TELEMETRY, "telemetry_rbac_fr")
    rbac_tool, cluster_app, rbac_file_name, certs_location, certs, client_cert, server_cert, rbac_user, bad_rbac_user, dut_hostname = setup

    try:
        rbac_tool.import_rbac_file(rbac_file_name, RbacConsts.NMX_RBAC_FILE_USER_PATH)
        rbac_tool.update_rbac_file(rbac_file_name)
        rbac_tool.update_rbac_mode(RbacConsts.RBAC_MODE_USERNAME_PASSWORD)

        with allure.step("verify RBAC works before factory reset"):
            rbac_tool.run_app_client(dut_hostname, rbac_user, client_cert, server_cert, expect_success=True)
            rbac_tool.run_app_client(dut_hostname, bad_rbac_user, client_cert, server_cert, expect_success=False)

        NvueGeneralCli.save_config(engines.dut)

        yield  # factory reset

        _verify_rbac_reset(setup)

    finally:
        _cleanup_nmx_rbac_certs(rbac_tool, certs_location, certs)

    yield


def _verify_rbac_preserved(setup_context: tuple):
    """Verify RBAC config is preserved after factory reset with keep-all-config."""
    rbac_tool, cluster_app, rbac_file_name, _, _, client_cert, server_cert, rbac_user, bad_rbac_user, dut_hostname = setup_context
    enable_cluster()

    with allure.step("re-setup certs for verification"):
        rbac_tool.prepare_nmx_certs([server_cert], [client_cert])

    with allure.step("verify RBAC still works after factory reset (config preserved)"):
        rbac_tool.run_app_client(dut_hostname, rbac_user, client_cert, server_cert, expect_success=True)
        rbac_tool.run_app_client(dut_hostname, bad_rbac_user, client_cert, server_cert, expect_success=False)

    with allure.step("restore RBAC mode and file"):
        rbac_tool.restore_rbac_mode()
        rbac_tool.restore_rbac_file()

    with allure.step("verify both users can connect after restore (RBAC disabled)"):
        rbac_tool.run_app_client(dut_hostname, rbac_user, client_cert, server_cert, expect_success=True)
        rbac_tool.run_app_client(dut_hostname, bad_rbac_user, client_cert, server_cert, expect_success=True)


def nmx_controller_rbac_factory_reset_keep_all_config_check() -> Generator[None, None, None]:
    """Verify NMX Controller RBAC configuration is preserved after factory reset with keep-all-config."""
    engines = TestToolkit.engines
    devices = TestToolkit.devices

    if not devices.dut.has_nmx:
        yield
        yield
        return

    setup = _setup_nmx_rbac(ClusterConsts.NMX_CONTROLLER, "controller_rbac_fr_kac")
    rbac_tool, _, rbac_file_name, certs_location, certs, client_cert, server_cert, rbac_user, bad_rbac_user, dut_hostname = setup

    try:
        rbac_tool.import_rbac_file(rbac_file_name, RbacConsts.NMX_RBAC_FILE_USER_PATH)
        rbac_tool.update_rbac_file(rbac_file_name)
        rbac_tool.update_rbac_mode(RbacConsts.RBAC_MODE_USERNAME_PASSWORD)

        with allure.step("verify RBAC works before factory reset"):
            rbac_tool.run_app_client(dut_hostname, rbac_user, client_cert, server_cert, expect_success=True)
            rbac_tool.run_app_client(dut_hostname, bad_rbac_user, client_cert, server_cert, expect_success=False)

        NvueGeneralCli.save_config(engines.dut)

        yield  # factory reset with keep-all-config

        _verify_rbac_preserved(setup)

    finally:
        _cleanup_nmx_rbac_certs(rbac_tool, certs_location, certs)

    yield


def nmx_telemetry_rbac_factory_reset_keep_all_config_check() -> Generator[None, None, None]:
    """Verify NMX Telemetry RBAC configuration is preserved after factory reset with keep-all-config."""
    engines = TestToolkit.engines
    devices = TestToolkit.devices

    if not devices.dut.has_nmx:
        yield
        yield
        return

    setup = _setup_nmx_rbac(ClusterConsts.NMX_TELEMETRY, "telemetry_rbac_fr_kac")
    rbac_tool, _, rbac_file_name, certs_location, certs, client_cert, server_cert, rbac_user, bad_rbac_user, dut_hostname = setup

    try:
        rbac_tool.import_rbac_file(rbac_file_name, RbacConsts.NMX_RBAC_FILE_USER_PATH)
        rbac_tool.update_rbac_file(rbac_file_name)
        rbac_tool.update_rbac_mode(RbacConsts.RBAC_MODE_USERNAME_PASSWORD)

        with allure.step("verify RBAC works before factory reset"):
            rbac_tool.run_app_client(dut_hostname, rbac_user, client_cert, server_cert, expect_success=True)
            rbac_tool.run_app_client(dut_hostname, bad_rbac_user, client_cert, server_cert, expect_success=False)

        NvueGeneralCli.save_config(engines.dut)

        yield  # factory reset with keep-all-config

        _verify_rbac_preserved(setup)

    finally:
        _cleanup_nmx_rbac_certs(rbac_tool, certs_location, certs)

    yield
