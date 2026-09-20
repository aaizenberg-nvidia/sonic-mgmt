"""
NV Bridge Encryption Tests.

Tests for verifying system and cluster internal encryption functionality.

Test setup: NVL6 setups
Preconditions:
- The switch completed the init flow
- Cluster apps are started
- NV Bridge has to be configured
"""

from __future__ import annotations

import logging
import time

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ApiType, ClusterApps
from ngts.nvos_tools.infra.NvBridgeTool import NvBridgeTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OpenSslSClient import OpenSslSClient
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts as ClusterTestConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.nmx_cert.constants import (
    NV_BRIDGE_SERVER_PORT,
    EncryptionMode,
)
from ngts.tests_nvos.general.security.nmx_cert.helpers import (
    enable_cluster,
    enable_cluster_app_manager_state,
    verify_manager_show,
)
from ngts.tests_nvos.general.security.nv_bridge.helpers import (
    BRIDGE_CROSS_FUNC_REBOOT_SETTLE_TIME,
    BridgeCrlCerts,
    BridgeSpiffeCerts,
    configure_bridge_internal_mtls,
    reset_cluster_app,
    rotate_cluster_internal_and_verify,
    rotate_system_internal_and_verify,
    save_config_and_reboot,
    update_cluster_internal_encryption,
    verify_cluster_app_internal_cert_files,
    verify_cluster_app_internal_crl_show,
    verify_cluster_app_internal_has_empty_defaults,
    verify_cluster_app_internal_json,
    verify_cluster_app_internal_show,
    verify_cluster_app_internal_spiffe_json,
    verify_nv_bridge_has_connection,
    verify_system_internal_cert_files,
    verify_system_internal_crl_show,
    verify_system_internal_has_empty_defaults,
    verify_system_internal_json,
    verify_system_internal_show,
    verify_system_internal_spiffe_json,
    wait_for_cluster_app_update,
)

logger = logging.getLogger(__name__)


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nv_bridge
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_bridge_system_encryption_show(engines, devices, test_api):
    """
    Test Objective: Verify system internal encryption default field and values

    Test Flow:
    1. Run nv show system internal - Verify command has default empty fields
    """
    TestToolkit.tested_api = test_api

    with allure.step("Verify nv show system internal has default empty fields"):
        verify_system_internal_show()


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nmx
@pytest.mark.usefixtures("disable_cluster")
@pytest.mark.parametrize("app_name", [ClusterApps.NMX_CONTROLLER])
def test_bridge_cluster_encryption_show(engines, devices, random_api, app_name):
    """
    Test Objective: Verify cluster internal encryption default field and values

    Test Flow:
    1. Run nv show cluster apps <app> internal - Verify command is pruned
       when cluster is disabled
    2. Enable cluster
    3. Run nv show cluster apps <app> internal - Verify command has default
       empty fields
    """
    TestToolkit.tested_api = random_api

    with allure.step(f"Verify nv show cluster apps {app_name} internal is pruned when cluster is disabled"):
        verify_cluster_app_internal_show(app_name, expect_success=False)

    with allure.step("Enable cluster"):
        enable_cluster(force_wait=True)

    with allure.step(f"Verify nv show cluster apps {app_name} internal has default empty fields"):
        verify_cluster_app_internal_has_empty_defaults(app_name)


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nmx
@pytest.mark.usefixtures("restore_system_internal_config")
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_bridge_system_encryption_set(
    engines,
    devices,
    test_api,
    import_certs_with_alt: tuple[CertInfo, CertInfo, CertInfo],
):
    """
    Test Objective: Verify system internal encryption action update works as
    expected

    Test Flow:
    1. Import certificate and ca-certificate (done by fixture)
    2. Enable cluster and start cluster apps
    3. Run nv show system internal - Verify command has default empty fields
    4. nv action update system internal certificate - Update system internal
       certificate and verify show and verify cert at path exists
    5. nv action update system internal ca-certificate - Update system internal
       ca-certificate and verify show and verify ca-cert at path exists
    6. nv action update system internal encryption - Update system internal
       encryption and verify show
    7. Run nv show system internal - Verify command has all the required values
    """
    TestToolkit.tested_api = test_api
    system = System()
    with allure.step("Import bridge certificate"):
        bridge_cert, bridge_alt_cert, _ = import_certs_with_alt

    with allure.step("Verify nv show system internal has default empty fields"):
        verify_system_internal_show()

    with allure.step("Update system internal certificate"):
        system.internal.certificate.action_update(bridge_cert.name).verify_result()
        with allure.step("Verify show reflects certificate update"):
            verify_system_internal_show(expect_cert=bridge_cert.name)
        with allure.step("Verify certificate file exists"):
            verify_system_internal_cert_files(engines.dut, expected_cert_id=bridge_cert.name)

    with allure.step("Update system internal ca-certificate"):
        system.internal.ca_certificate.action_update(bridge_cert.cacert_name).verify_result()
        with allure.step("Verify show reflects ca-certificate update"):
            verify_system_internal_show(expect_cert=bridge_cert.name, expect_cacert=bridge_cert.cacert_name)
        with allure.step("Verify ca-certificate file exists"):
            verify_system_internal_cert_files(
                engines.dut,
                expected_cert_id=bridge_cert.name,
                expected_cacert_id=bridge_cert.cacert_name,
            )

    with allure.step("Update system internal encryption to mtls"):
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        with allure.step("Verify show reflects encryption update"):
            verify_system_internal_show(
                expect_cert=bridge_cert.name,
                expect_cacert=bridge_cert.cacert_name,
                expect_encryption=EncryptionMode.MTLS,
            )
            verify_system_internal_json(
                engines.dut,
                expected_cert=bridge_cert.name,
                expected_cacert=bridge_cert.cacert_name,
                expected_encryption=EncryptionMode.MTLS,
            )

    with allure.step("Verify nv show system internal has all required values"):
        verify_system_internal_show(
            expect_cert=bridge_cert.name,
            expect_cacert=bridge_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
        )


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nmx
@pytest.mark.usefixtures("enable_cluster")
def test_bridge_cluster_encryption_set(
    engines,
    devices,
    random_api,
    restore_cluster_app_internal_config,
    import_certs_with_alt: tuple[CertInfo, CertInfo, CertInfo],
):
    """
    Test Objective: Verify cluster internal encryption actions work as expected

    Test Flow:
    1. Enable cluster and start cluster apps
    2. Import certificate and ca-certificate
    3. nv show cluster apps <app> internal - Verify command has default
       empty fields
    4. nv action update cluster apps <app> internal certificate - Update
       cluster internal certificate and verify show and verify cert at
       path exists
    5. nv action update cluster apps <app> internal ca-certificate - Update
       cluster internal ca-certificate and verify show and verify ca-cert
       at path exists
    6. nv action update cluster apps <app> internal encryption - Update
       cluster internal encryption and verify show
    7. nv show cluster apps <app> internal - Verify all updated fields
       are seen
    """
    TestToolkit.tested_api = random_api
    cluster = Cluster()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    bridge_cert, bridge_alt_cert, _ = import_certs_with_alt

    with allure.step(f"Verify nv show cluster apps {app_name} internal has default empty fields"):
        verify_cluster_app_internal_has_empty_defaults(app_name)

    with allure.step(f"Update cluster apps {app_name} internal certificate"):
        app.internal.certificate.action_update(bridge_cert.name).verify_result()
        with allure.step("Verify show reflects certificate update"):
            verify_cluster_app_internal_show(app_name, expect_cert=bridge_cert.name)

        with allure.step("Verify certificate file exists"):
            verify_cluster_app_internal_cert_files(app_name, engines.dut, expected_cert_id=bridge_cert.name)

    with allure.step(f"Update cluster apps {app_name} internal ca-certificate"):
        app.internal.ca_certificate.action_update(bridge_cert.cacert_name).verify_result()
        with allure.step("Verify show reflects ca-certificate update"):
            verify_cluster_app_internal_show(
                app_name,
                expect_cert=bridge_cert.name,
                expect_cacert=bridge_cert.cacert_name,
            )
        with allure.step("Verify ca-certificate file exists"):
            verify_cluster_app_internal_cert_files(
                app_name,
                engines.dut,
                expected_cert_id=bridge_cert.name,
                expected_cacert_id=bridge_cert.cacert_name,
            )

    with allure.step(f"Update cluster apps {app_name} internal encryption to mtls"):
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        # Enabling cluster app internal mTLS on its own no longer brings nmxc-conn down: nmx-c stays up
        # even while system internal is still at its default (system internal config is not required for
        # the cluster app internal connection to remain up).
        ClusterTools.wait_for_apps_to_be_in_wanted_state(
            cluster,
            cluster_expected_state="enabled",
            nmx_c_expected_state="up",
            engine=engines.dut,
        )
        time.sleep(1)
        with allure.step("Verify show reflects encryption update"):
            verify_cluster_app_internal_show(
                app_name,
                expect_cert=bridge_cert.name,
                expect_cacert=bridge_cert.cacert_name,
                expect_encryption=EncryptionMode.MTLS,
            )

    with allure.step(f"Verify nv show cluster apps {app_name} internal shows all updated fields"):
        verify_cluster_app_internal_show(
            app_name,
            expect_cert=bridge_cert.name,
            expect_cacert=bridge_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
        )


def _verify_bridge_connection_up(cluster: Cluster, engine) -> None:
    ClusterTools.wait_for_apps_to_be_in_wanted_state(
        cluster,
        cluster_expected_state="enabled",
        nmx_c_expected_state="up",
        engine=engine,
    )
    verify_nv_bridge_has_connection(engine, expect_connection=True)


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nmx
@pytest.mark.reboot
@pytest.mark.track_serial_console
@pytest.mark.usefixtures(
    "enable_cluster",
    "restore_cluster_app_internal_config",
    "restore_system_internal_config",
)
def test_bridge_cluster_internal_mtls_reboot_connectivity(
    engines,
    random_api,
    restore_cluster_app_internal_config,
    import_certs_with_alt: tuple[CertInfo, CertInfo, CertInfo],
):
    """
    Test Objective: Verify cluster internal mTLS connection is restored after reboot.

    Test Flow:
    1. Configure system + cluster internal mTLS
    2. Verify nv-bridge connection is up
    3. Save config and reboot
    4. Verify nv-bridge connection comes back up
    """
    TestToolkit.tested_api = random_api
    cluster = Cluster()
    system = System()
    app_name = restore_cluster_app_internal_config
    bridge_cert, _, _ = import_certs_with_alt

    configure_bridge_internal_mtls(system, cluster, app_name, bridge_cert, engines.dut)

    with allure.step("Verify connection is up before reboot"):
        _verify_bridge_connection_up(cluster, engines.dut)

    save_config_and_reboot(engines)

    with allure.step("Verify connection comes back up after reboot"):
        _verify_bridge_connection_up(cluster, engines.dut)


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nmx
@pytest.mark.reboot
@pytest.mark.track_serial_console
# Two full reboots (~290s each) plus their post-reboot settle time already exceed the
# session-wide `--timeout 900`, before any certificate / log-stream / SDN work is counted.
@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
@pytest.mark.usefixtures(
    "enable_cluster",
    "restore_cluster_app_internal_config",
    "restore_system_internal_config",
)
def test_bridge_cluster_internal_mtls_reboot_cross_functionality(
    engines,
    devices,
    test_api,
    restore_cluster_app_internal_config,
    import_certs_with_alt: tuple[CertInfo, CertInfo, CertInfo],
    import_manager_certs: dict[str, CertInfo],
):
    """
    Test Objective: Verify internal mTLS + manager mTLS / log stream / SDN survive reboot,
    and that disabling mTLS persists across a second reboot while the connection stays up.

    Test Flow:
    1. Configure system/cluster internal mTLS and verify connection
    2. Configure NMX manager mTLS, NMX-C log stream, and SDN generate
    3. Save+reboot; verify shows and connection
    4. Disable all mTLS, save+reboot; verify disabled state and healthy connection
    """
    TestToolkit.tested_api = test_api
    cluster = Cluster()
    system = System()
    sdn = Sdn()
    app_name = restore_cluster_app_internal_config
    bridge_cert, _, _ = import_certs_with_alt

    configure_bridge_internal_mtls(system, cluster, app_name, bridge_cert, engines.dut)
    with allure.step("Verify connection is up before cross functionality"):
        _verify_bridge_connection_up(cluster, engines.dut)

    player = engines.sonic_mgmt
    protocol = ClusterTestConsts.PROTOCOL_RSYSLOG
    port = ClusterTestConsts.NMXC_LOG_STREAM_PORT[protocol]
    # Product expects rsyslog remote-url as hostname:port (no credentials).
    stream = f"{protocol} {player.ip}:{port}"
    stream_show = f"{player.ip}:{port}"

    with allure.step("Configure manager mTLS, log stream, and SDN"):
        for mgr_app, cert in import_manager_certs.items():
            if mgr_app not in devices.dut.expected_cluster_apps:
                continue
            manager = cluster.apps.app_name[mgr_app].manager
            enable_cluster_app_manager_state(manager)
            manager.certificate.action_update(cert.name).verify_result()
            manager.ca_certificate.action_update(cert.cacert_name).verify_result()
            manager.encryption.action_update(EncryptionMode.MTLS).verify_result()
        cluster.apps.app_name[ClusterApps.NMX_CONTROLLER].logstream.action_update_cluster_log_stream(
            stream=stream,
        ).verify_result()
        # Generates SDN files and validates them via show (existing ClusterTools helper).
        ClusterTools.get_all_apps_config_files_paths(sdn, devices, engine=engines.dut)

    save_config_and_reboot(engines, settle_time=BRIDGE_CROSS_FUNC_REBOOT_SETTLE_TIME)

    with allure.step("Verify mTLS + cross-func persisted after first reboot"):
        verify_system_internal_show(
            expect_cert=bridge_cert.name,
            expect_cacert=bridge_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
            dut_engine=engines.dut,
        )
        verify_cluster_app_internal_show(
            app_name,
            expect_cert=bridge_cert.name,
            expect_cacert=bridge_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
            dut_engine=engines.dut,
        )
        for mgr_app, cert in import_manager_certs.items():
            if mgr_app not in devices.dut.expected_cluster_apps:
                continue
            verify_manager_show(
                mgr_app,
                expect_cert=cert.name,
                expect_cacert=cert.cacert_name,
                expect_encryption=EncryptionMode.MTLS,
            )
        logstream_out = cluster.apps.app_name[ClusterApps.NMX_CONTROLLER].logstream.show(
            exempted_err_msgs="Error",
        )
        assert "The requested item does not exist." not in logstream_out, "NMX-C log stream missing after reboot"
        parsed = OutputParsingTool.parse_json_str_to_dictionary(logstream_out).get_returned_value()
        assert parsed["protocol"] == protocol, f"Unexpected log stream protocol: {parsed['protocol']}"
        assert parsed["remote-url"] == stream_show, f"Unexpected log stream remote-url: {parsed['remote-url']}"
        _verify_bridge_connection_up(cluster, engines.dut)

    with allure.step("Disable all mTLS"):
        system.internal.encryption.action_update(EncryptionMode.DISABLED).verify_result()
        update_cluster_internal_encryption(cluster, app_name, EncryptionMode.DISABLED, engines.dut)
        for mgr_app in import_manager_certs:
            if mgr_app not in devices.dut.expected_cluster_apps:
                continue
            cluster.apps.app_name[mgr_app].manager.encryption.action_update(
                EncryptionMode.DISABLED,
            ).verify_result()

    save_config_and_reboot(engines, settle_time=BRIDGE_CROSS_FUNC_REBOOT_SETTLE_TIME)

    with allure.step("Verify mTLS stayed disabled after second reboot, connection healthy"):
        verify_system_internal_show(
            expect_cert=bridge_cert.name,
            expect_cacert=bridge_cert.cacert_name,
            expect_encryption=EncryptionMode.DISABLED,
            dut_engine=engines.dut,
        )
        verify_cluster_app_internal_show(
            app_name,
            expect_cert=bridge_cert.name,
            expect_cacert=bridge_cert.cacert_name,
            expect_encryption=EncryptionMode.DISABLED,
            dut_engine=engines.dut,
        )
        for mgr_app, cert in import_manager_certs.items():
            if mgr_app not in devices.dut.expected_cluster_apps:
                continue
            verify_manager_show(
                mgr_app,
                expect_cert=cert.name,
                expect_cacert=cert.cacert_name,
                expect_encryption=EncryptionMode.DISABLED,
            )
        _verify_bridge_connection_up(cluster, engines.dut)


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nmx
@pytest.mark.usefixtures(
    "enable_cluster",
    "restore_cluster_app_internal_config",
    "restore_system_internal_config",
)
def test_bridge_cluster_rotation(
    engines,
    devices,
    random_api,
    restore_cluster_app_internal_config,
    import_certs_with_alt: tuple[CertInfo, CertInfo, CertInfo],
):
    """
    Test Objective: Verify certificate rotation flow with connection verification
    for both system internal and cluster internal encryption.

    Test Flow:
    1. Enable cluster and import certificates (done by fixtures)
    2. Configure system internal with cert, ca-cert, and mtls
    3. Configure cluster app with cert, ca-cert, and mtls
    4. Reset cluster app and verify initial connection
    5. Rotation cycle 1 - system internal:
       - Add alternate cert, rotate, verify connection
    6. Restore system alt-cert and verify connection
    7. Rotation cycle 2 - cluster internal:
       - Add alternate cert, rotate, verify connection
    8. Restore cluster alt-cert and verify connection
    9. Rotation cycle 3 - rotate system internal with reset
    10. Rotation cycle 4 - rotate cluster internal with reset
    """
    TestToolkit.tested_api = random_api
    cluster = Cluster()
    system = System()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    bridge_cert, bridge_alt_cert, _ = import_certs_with_alt

    with allure.step("Configure system internal with cert, ca-cert, mtls"):
        system.internal.certificate.action_update(bridge_cert.name).verify_result()
        system.internal.ca_certificate.action_update(bridge_cert.cacert_name).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()

        with allure.step("Verify system internal configuration"):
            verify_system_internal_show(
                expect_cert=bridge_cert.name,
                expect_cacert=bridge_cert.cacert_name,
                expect_encryption=EncryptionMode.MTLS,
            )
            verify_system_internal_cert_files(
                engines.dut,
                expected_cert_id=bridge_cert.name,
                expected_cacert_id=bridge_cert.cacert_name,
            )

    with allure.step(f"Configure cluster {app_name} with cert, ca-cert, mtls"):
        app.internal.certificate.action_update(bridge_cert.name).verify_result()
        app.internal.ca_certificate.action_update(bridge_cert.cacert_name).verify_result()
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()

        with allure.step(f"Verify cluster {app_name} internal configuration"):
            verify_cluster_app_internal_show(
                app_name,
                expect_cert=bridge_cert.name,
                expect_cacert=bridge_cert.cacert_name,
                expect_encryption=EncryptionMode.MTLS,
            )
            verify_cluster_app_internal_cert_files(
                app_name,
                engines.dut,
                expected_cert_id=bridge_cert.name,
                expected_cacert_id=bridge_cert.cacert_name,
            )

    with allure.step("Reset cluster app and verify initial connection"):
        reset_cluster_app(cluster, app_name, engines.dut)
        verify_nv_bridge_has_connection(engines.dut, expect_connection=True)

    with allure.step("Rotation cycle 1 - rotate system internal"):
        system.internal.alternate_certificate.action_update(bridge_alt_cert.name).verify_result()
        verify_system_internal_show(
            expect_cert=bridge_cert.name,
            expect_cacert=bridge_cert.cacert_name,
            expect_alt_cert=bridge_alt_cert.name,
            expect_encryption=EncryptionMode.MTLS,
        )
        verify_system_internal_cert_files(
            engines.dut,
            expected_cert_id=bridge_cert.name,
            expected_cacert_id=bridge_cert.cacert_name,
            expected_alt_cert_id=bridge_alt_cert.name,
        )
        current_sys_cert = bridge_cert.name
        current_sys_alt = bridge_alt_cert.name
        current_sys_cert, current_sys_alt = rotate_system_internal_and_verify(
            system,
            cluster,
            app_name,
            engines.dut,
            bridge_cert.cacert_name,
            current_sys_cert,
            current_sys_alt,
            EncryptionMode.MTLS,
        )
        verify_nv_bridge_has_connection(engines.dut, expect_connection=True)

    with allure.step("Restore system alt-cert and verify connection"):
        system.internal.alternate_certificate.action_restore().verify_result()
        wait_for_cluster_app_update(cluster, engines.dut)
        verify_nv_bridge_has_connection(engines.dut, expect_connection=True)

    with allure.step("Rotation cycle 2 - rotate cluster internal"):
        app.internal.alternate_certificate.action_update(bridge_alt_cert.name).verify_result()
        verify_cluster_app_internal_show(
            app_name,
            expect_cert=bridge_cert.name,
            expect_cacert=bridge_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
            expect_alt_cert=bridge_alt_cert.name,
        )
        verify_cluster_app_internal_cert_files(
            app_name,
            engines.dut,
            expected_cert_id=bridge_cert.name,
            expected_cacert_id=bridge_cert.cacert_name,
            expected_alt_cert_id=bridge_alt_cert.name,
        )
        current_cluster_cert = bridge_cert.name
        current_cluster_alt = bridge_alt_cert.name
        current_cluster_cert, current_cluster_alt = rotate_cluster_internal_and_verify(
            app,
            cluster,
            app_name,
            engines.dut,
            bridge_cert.cacert_name,
            current_cluster_cert,
            current_cluster_alt,
            EncryptionMode.MTLS,
        )
        verify_nv_bridge_has_connection(engines.dut, expect_connection=True)

    with allure.step("Restore cluster alt-cert and verify connection"):
        app.internal.alternate_certificate.action_restore().verify_result()
        wait_for_cluster_app_update(cluster, engines.dut)
        verify_nv_bridge_has_connection(engines.dut, expect_connection=True)

    with allure.step("Rotation cycle 3 - rotate system internal with reset"):
        system.internal.alternate_certificate.action_update(bridge_cert.name).verify_result()
        current_sys_cert, current_sys_alt = rotate_system_internal_and_verify(
            system,
            cluster,
            app_name,
            engines.dut,
            bridge_cert.cacert_name,
            current_sys_cert,
            current_sys_alt,
            EncryptionMode.MTLS,
            should_reset=True,
        )

    with allure.step("Rotation cycle 4 - rotate cluster internal with reset"):
        app.internal.alternate_certificate.action_update(bridge_cert.name).verify_result()
        current_cluster_cert, current_cluster_alt = rotate_cluster_internal_and_verify(
            app,
            cluster,
            app_name,
            engines.dut,
            bridge_cert.cacert_name,
            current_cluster_cert,
            current_cluster_alt,
            EncryptionMode.MTLS,
            should_reset=True,
        )

    logger.info(
        f"Rotation flow completed. "
        f"System cert: {current_sys_cert}, alt: {current_sys_alt}. "
        f"Cluster cert: {current_cluster_cert}, alt: {current_cluster_alt}."
    )


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nmx
@pytest.mark.usefixtures("enable_cluster", "restore_system_internal_config")
def test_bridge_main_flow(
    engines,
    devices,
    random_api,
    import_certs_with_alt: tuple[CertInfo, CertInfo, CertInfo],
    restore_cluster_app_internal_config,
):
    """
    Test Objective: Verify NV Bridge encryption main flow with grpc requests.

    Test Flow:
    1. Enable cluster and start cluster apps (done by fixture)
    2. Import certs and ca-certs (done by fixture)
    3. Update cluster internal with certificate, ca-certificate, and mtls
    4. Verify basic grpc hello request to bridge (plaintext)
    5. Configure NV Bridge (system internal) with certificate, ca-certificate
       and mtls
    6. Perform encrypted grpc request to bridge
    """
    TestToolkit.tested_api = random_api
    cluster = Cluster()
    system = System()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    bridge_cert, bridge_alt_cert, _ = import_certs_with_alt

    nv_bridge_tool = NvBridgeTool(host=engines.dut.ip)

    with allure.step("Update cluster internal with cert, ca-cert, mtls"):
        app.internal.certificate.action_update(bridge_cert.name).verify_result()
        app.internal.ca_certificate.action_update(bridge_cert.cacert_name).verify_result()
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        # Enabling cluster app internal mTLS on its own no longer brings nmxc-conn down: nmx-c stays up
        # even while system internal is still at its default (system internal config is not required for
        # the cluster app internal connection to remain up).
        ClusterTools.wait_for_apps_to_be_in_wanted_state(
            cluster,
            cluster_expected_state="enabled",
            nmx_c_expected_state="up",
            engine=engines.dut,
        )
        time.sleep(1)

        with allure.step("Verify cluster internal configuration"):
            verify_cluster_app_internal_show(
                app_name,
                expect_cert=bridge_cert.name,
                expect_cacert=bridge_cert.cacert_name,
                expect_encryption=EncryptionMode.MTLS,
            )
            verify_cluster_app_internal_json(
                engines.dut,
                app_name,
                expected_cert=bridge_cert.name,
                expected_cacert=bridge_cert.cacert_name,
                expected_encryption=EncryptionMode.MTLS,
            )

    with allure.step("Verify basic grpc hello request to bridge (plaintext)"):
        nv_bridge_tool.run_bridge_hello(plaintext=True, expect_success=True)

    with allure.step("Configure NV Bridge with cert, ca-cert, mtls"):
        system.internal.certificate.action_update(bridge_cert.name).verify_result()
        system.internal.ca_certificate.action_update(bridge_cert.cacert_name).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        wait_for_cluster_app_update(cluster, engines.dut)

        with allure.step("Verify system internal configuration"):
            verify_system_internal_show(
                expect_cert=bridge_cert.name,
                expect_cacert=bridge_cert.cacert_name,
                expect_encryption=EncryptionMode.MTLS,
            )
            verify_system_internal_json(
                engines.dut,
                expected_cert=bridge_cert.name,
                expected_cacert=bridge_cert.cacert_name,
                expected_encryption=EncryptionMode.MTLS,
            )

    with allure.step("Perform encrypted grpc request to bridge"):
        nv_bridge_tool.run_bridge_hello(
            client_cert=bridge_cert,
            client_cacert=bridge_cert,
            plaintext=False,
            expect_success=True,
        )


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nmx
@pytest.mark.usefixtures("enable_cluster")
def test_bridge_tls_handshake_verification(
    engines,
    devices,
    random_api,
    import_certs_with_alt: tuple[CertInfo, CertInfo, CertInfo],
    restore_cluster_app_internal_config,
    restore_system_internal_config,
):
    """
    Test Objective: Verify TLS handshake using OpenSSL s_client after
    configuring cluster and system internal encryption.

    Test Flow:
    1. Enable cluster and start cluster apps (done by fixture)
    2. Import certs and ca-certs (done by fixture)
    3. Configure cluster internal with cert, ca-cert, and mtls
    4. Verify TLS handshake on cluster internal port (9381) succeeds
    5. Verify TLS handshake on cluster internal port fails with invalid certs
    6. Configure system internal with cert, ca-cert, and mtls
    7. Verify TLS handshake on system internal port (50052) succeeds
    8. Verify TLS handshake on system internal port fails with invalid certs
    """
    TestToolkit.tested_api = random_api
    cluster = Cluster()
    system = System()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    bridge_cert, _, mismatched_cert = import_certs_with_alt
    dut_ip = engines.dut.ip

    with allure.step("Configure cluster internal with cert, ca-cert, mtls"):
        wait_for_cluster_app_update(cluster, engines.dut)
        app.internal.certificate.action_update(bridge_cert.name).verify_result()
        app.internal.ca_certificate.action_update(bridge_cert.cacert_name).verify_result()
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()

        verify_cluster_app_internal_show(
            app_name,
            expect_cert=bridge_cert.name,
            expect_cacert=bridge_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
        )

    # acl_name = "AAA-nmx-c-verification"
    # acl_obj = open_port_via_sys_control_plane_acl(acl_name, NV_BRIDGE_CLIENT_PORT)

    # cluster_client = OpenSslSClient(dut_ip, NV_BRIDGE_CLIENT_PORT)

    # with allure.step("Verify TLS handshake on cluster internal port succeeds"):
    #     result = cluster_client.verify_successful_handshake(
    #         ca_file=bridge_cert.cacert,
    #         cert=bridge_cert.public,
    #         key=bridge_cert.private,
    #     )
    #     logger.info(
    #         f"Cluster internal TLS verified: return_code={result.return_code}, tls={result.tls_version}, alpn={result.alpn_protocol}"
    #     )

    # with allure.step("Verify TLS handshake fails with invalid certs"):
    #     _verify_tls_handshake_negative_scenarios(cluster_client, bridge_cert, mismatched_cert, "Cluster")

    with allure.step("Configure system internal with cert, ca-cert, mtls"):
        system.internal.certificate.action_update(bridge_cert.name).verify_result()
        system.internal.ca_certificate.action_update(bridge_cert.cacert_name).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        wait_for_cluster_app_update(cluster, engines.dut)
        # Need additional 2 sec for bridge to be fully encrypted
        time.sleep(2)

        verify_system_internal_show(
            expect_cert=bridge_cert.name,
            expect_cacert=bridge_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
        )

    system_client = OpenSslSClient(dut_ip, NV_BRIDGE_SERVER_PORT)

    with allure.step("Verify TLS handshake on system internal port succeeds"):
        result = system_client.verify_successful_handshake(
            ca_file=bridge_cert.cacert,
            cert=bridge_cert.public,
            key=bridge_cert.private,
            alpn="h2",
        )
        logger.info(
            f"System internal TLS verified: return_code={result.return_code}, tls={result.tls_version}, alpn={result.alpn_protocol}"
        )

    with allure.step("Verify TLS handshake fails with invalid certs"):
        _verify_tls_handshake_negative_scenarios(system_client, bridge_cert, mismatched_cert, "Bridge server")


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nmx
@pytest.mark.usefixtures("enable_cluster", "restore_system_internal_config")
def test_bridge_cluster_encryption_negative(
    engines,
    devices,
    random_api,
    import_certs_with_alt: tuple[CertInfo, CertInfo, CertInfo],
    restore_cluster_app_internal_config,
):
    """
    Test Objective: Verify all negative nv-bridge cases for cluster.

    Test Flow:
    1. Enable cluster and start cluster apps (done by fixture)
    2. Import certs and ca-certs (done by fixture)
    3. Verify nv show cluster apps <app> internal has default empty fields
    4. Try to enable mtls encryption without cert and ca-cert - expect fail
    5. Update cluster cert only
    6. Try to enable mtls encryption without ca-cert - expect fail
    7. Update ca-cert that didn't sign the cert - expect fail
    8. Update cluster cert and matching ca-cert - expect success
    9. Set system with another cert and ca-cert (mismatched)
    10. Perform request with system cert - expect fail
    11. Perform request with cluster cert - expect fail
    """
    TestToolkit.tested_api = random_api
    cluster = Cluster()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    bridge_cert, bridge_alt_cert, mismatched_cert = import_certs_with_alt

    with allure.step(f"Verify nv show cluster apps {app_name} internal has default empty fields"):
        verify_cluster_app_internal_has_empty_defaults(app_name)

    with allure.step("Try to enable mtls encryption without cert and ca-cert - expect fail"):
        result = app.internal.encryption.action_update(EncryptionMode.MTLS)
        result.verify_result(should_succeed=False)

    with allure.step(f"Update cluster apps {app_name} internal certificate only"):
        app.internal.certificate.action_update(bridge_cert.name).verify_result()

    with allure.step("Try to enable mtls encryption without ca-cert - expect fail"):
        result = app.internal.encryption.action_update(EncryptionMode.MTLS)
        result.verify_result(should_succeed=False)

    with allure.step("Update ca-cert that didn't sign the cert - expect fail on applying mtls"):
        result = app.internal.ca_certificate.action_update(mismatched_cert.cacert_name)
        result.verify_result()

    with allure.step("Try to enable mtls encryption with incorrect ca-cert - expect fail"):
        result = app.internal.encryption.action_update(EncryptionMode.MTLS)
        result.verify_result(should_succeed=False)

    with allure.step(f"Update cluster apps {app_name} with matching cert and ca-cert"):
        app.internal.ca_certificate.action_update(bridge_cert.cacert_name).verify_result()
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        # Enabling cluster app internal mTLS on its own no longer brings nmxc-conn down: nmx-c stays up
        # even while system internal is still at its default (system internal config is not required for
        # the cluster app internal connection to remain up).
        ClusterTools.wait_for_apps_to_be_in_wanted_state(
            cluster,
            cluster_expected_state="enabled",
            nmx_c_expected_state="up",
            engine=engines.dut,
        )
        time.sleep(1)


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nmx
@pytest.mark.usefixtures("restore_system_internal_config")
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_bridge_system_encryption_negative(
    engines,
    devices,
    test_api,
    import_certs: tuple[CertInfo, CertInfo],
):
    """
    Test Objective: Verify all negative nv-bridge cases for system.

    Test Flow:
    1. Import certs and ca-certs (done by fixture)
    2. Enable cluster and start cluster apps
    3. Verify nv show system internal has default empty fields
    4. Try to update mtls encryption without cert and ca-cert - expect fail
    5. Update system cert only
    6. Try to update mtls encryption without ca-cert - expect fail
    7. Update ca-cert that didn't sign the cert - expect fail
    """
    TestToolkit.tested_api = test_api
    system = System()
    bridge_cert, mismatched_cert = import_certs

    with allure.step("Verify nv show system internal has default empty fields"):
        verify_system_internal_has_empty_defaults()

    with allure.step("Try to update mtls encryption without cert and ca-cert - expect fail"):
        result = system.internal.encryption.action_update(EncryptionMode.MTLS)
        result.verify_result(should_succeed=False)

    with allure.step("Update system cert only"):
        system.internal.certificate.action_update(bridge_cert.name).verify_result()

    with allure.step("Try to update mtls encryption without ca-cert - expect fail"):
        result = system.internal.encryption.action_update(EncryptionMode.MTLS)
        result.verify_result(should_succeed=False)

    with allure.step("Update ca-cert that didn't sign the cert"):
        result = system.internal.ca_certificate.action_update(mismatched_cert.cacert_name)
        result.verify_result()

    with allure.step("Try to update mtls encryption with bad ca-cert - expect fail"):
        result = system.internal.encryption.action_update(EncryptionMode.MTLS)
        result.verify_result(should_succeed=False)


def _verify_tls_handshake_negative_scenarios(
    client: OpenSslSClient,
    valid_cert: CertInfo,
    invalid_cert: CertInfo,
    service_name: str,
) -> None:
    """
    Verify TLS handshake fails with various invalid certificate scenarios.

    Args:
        client: OpenSslSClient instance to use for verification.
        valid_cert: Valid certificate that should work.
        invalid_cert: Invalid/mismatched certificate for negative tests.
        service_name: Name of service for logging (e.g., "Cluster", "System").
    """
    with allure.step(f"Verify {service_name} TLS fails with wrong client cert"):
        result = client.verify_handshake_fails(
            ca_file=valid_cert.cacert,
            cert=invalid_cert.public,
            key=invalid_cert.private,
            expected_error=r"(unknown ca|alert)",
        )
        logger.info(f"{service_name} TLS failure (wrong cert): return_code={result.return_code}, error={result.error_message}")

    with allure.step(f"Verify {service_name} TLS fails with wrong CA"):
        result = client.verify_handshake_fails(
            ca_file=invalid_cert.cacert,
            cert=valid_cert.public,
            key=valid_cert.private,
            expected_return_code=20,
            expected_error=r"unable to get local issuer certificate",
        )
        logger.info(f"{service_name} TLS failure (wrong CA): return_code={result.return_code}, error={result.error_message}")

    with allure.step(f"Verify {service_name} TLS fails with no client cert"):
        result = client.verify_handshake_fails(
            ca_file=valid_cert.cacert,
            expected_error=r"(certificate required|unknown ca|alert)",
        )
        logger.info(f"{service_name} TLS failure (no cert): return_code={result.return_code}, error={result.error_message}")

    with allure.step(f"Verify {service_name} TLS fails with no CA"):
        result = client.verify_handshake_fails(
            cert=valid_cert.public,
            key=valid_cert.private,
            expected_error=r"(self[- ]signed|unable to get local issuer|verify)",
        )
        logger.info(f"{service_name} TLS failure (no CA): return_code={result.return_code}, error={result.error_message}")


# ===========================================================================
# New CRL/SPIFFE tests (test plan 4.1 – 4.9)
# ===========================================================================


# ---------------------------------------------------------------------------
# 4.1 – CRL: System internal CRL show lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nv_bridge
@pytest.mark.crl
@pytest.mark.usefixtures("restore_system_internal_config")
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_bridge_system_internal_crl_show(
    engines,
    devices,
    test_api,
    import_crl_bridge_certs: BridgeCrlCerts,
    bridge_crl_factory,
):
    """
    Test Objective: Verify system internal CRL update, show, and restore.

    Test Flow:
    1. Verify system internal CRL show has default empty value
    2. Generate and import a CRL revoking a non-active cert
    3. Configure system internal with cert + CA (prerequisite for CRL binding)
    4. Bind CRL to system internal via action update
    5. Verify CRL name appears in show output
    6. Restore system internal CRL
    7. Verify CRL is cleared in show output
    """
    TestToolkit.tested_api = test_api
    system = System()

    primary_cert = import_crl_bridge_certs.primary_cert
    crl_name = "sys_internal_crl"

    with allure.step("Verify system internal CRL show has default empty value"):
        verify_system_internal_crl_show(expect_crl="")

    with allure.step("Generate and import CRL"):
        bridge_crl_factory(
            crl_name=crl_name,
            cert_to_revoke=import_crl_bridge_certs.revoked_primary_cert,
            certs_dir=import_crl_bridge_certs.certs_dir,
        )

    with allure.step("Configure system internal with cert + CA"):
        system.internal.certificate.action_update(primary_cert.name).verify_result()
        system.internal.ca_certificate.action_update(primary_cert.cacert_name).verify_result()

    with allure.step("Bind CRL to system internal"):
        system.internal.crl.action_update(crl_name).verify_result()

    with allure.step("Verify CRL name appears in show output"):
        verify_system_internal_crl_show(expect_crl=crl_name)

    with allure.step("Restore system internal CRL"):
        system.internal.crl.action_restore().verify_result()

    with allure.step("Verify CRL is cleared after restore"):
        verify_system_internal_crl_show(expect_crl="")


# ---------------------------------------------------------------------------
# 4.2 – CRL: Cluster app internal CRL show lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nmx
@pytest.mark.nv_bridge
@pytest.mark.crl
@pytest.mark.usefixtures("enable_cluster")
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_bridge_cluster_internal_crl_show(
    engines,
    devices,
    test_api,
    restore_cluster_app_internal_config,
    import_crl_bridge_certs: BridgeCrlCerts,
    bridge_crl_factory,
):
    """
    Test Objective: Verify cluster app internal CRL update, show, and restore.

    Test Flow:
    1. Verify cluster app internal CRL show has default empty value
    2. Generate and import a CRL revoking a non-active cert
    3. Configure cluster app internal with cert + CA (prerequisite for CRL binding)
    4. Bind CRL to cluster app internal via action update
    5. Verify CRL name appears in show output
    6. Restore cluster app internal CRL
    7. Verify CRL is cleared in show output
    """
    TestToolkit.tested_api = test_api
    cluster = Cluster()

    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    primary_cert = import_crl_bridge_certs.primary_cert
    crl_name = "cluster_internal_crl"

    with allure.step(f"Verify cluster app {app_name} internal CRL show has default empty value"):
        verify_cluster_app_internal_crl_show(app_name, expect_crl="")

    with allure.step("Generate and import CRL"):
        bridge_crl_factory(
            crl_name=crl_name,
            cert_to_revoke=import_crl_bridge_certs.revoked_primary_cert,
            certs_dir=import_crl_bridge_certs.certs_dir,
        )

    with allure.step(f"Configure cluster app {app_name} internal with cert + CA"):
        app.internal.certificate.action_update(primary_cert.name).verify_result()
        app.internal.ca_certificate.action_update(primary_cert.cacert_name).verify_result()

    with allure.step(f"Bind CRL to cluster app {app_name} internal"):
        app.internal.crl.action_update(crl_name).verify_result()

    with allure.step("Verify CRL name appears in show output"):
        verify_cluster_app_internal_crl_show(app_name, expect_crl=crl_name)

    with allure.step(f"Restore cluster app {app_name} internal CRL"):
        app.internal.crl.action_restore().verify_result()

    with allure.step("Verify CRL is cleared after restore"):
        verify_cluster_app_internal_crl_show(app_name, expect_crl="")


# ---------------------------------------------------------------------------
# 4.3 – CRL: Allow connection with empty CRL (unrelated cert revoked)
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nv_bridge
@pytest.mark.crl
@pytest.mark.usefixtures("enable_cluster", "restore_system_internal_config")
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_bridge_crl_allow_connection_empty_crl(
    engines,
    devices,
    test_api,
    restore_cluster_app_internal_config,
    import_crl_bridge_certs: BridgeCrlCerts,
    bridge_crl_factory,
):
    """
    Test Objective: CRL that revokes an unrelated cert should NOT block the
    active cert. Configure full mTLS + CRL on both system and cluster scopes.
    Bridge connection and gRPC hello should still work.

    Test Flow:
    1. Generate CRL revoking an unrelated cert
    2. Configure system internal: cert + CA + mTLS + CRL
    3. Configure cluster app internal: cert + CA + mTLS + CRL
    4. Reset cluster app and verify bridge connection
    5. Verify gRPC hello succeeds with the active cert
    """
    TestToolkit.tested_api = test_api
    cluster = Cluster()
    system = System()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    primary_cert = import_crl_bridge_certs.primary_cert
    crl_name = "bridge_unrelated_crl"

    with allure.step("Generate CRL revoking an unrelated cert"):
        bridge_crl_factory(
            crl_name=crl_name,
            cert_to_revoke=import_crl_bridge_certs.revoked_primary_cert,
            certs_dir=import_crl_bridge_certs.certs_dir,
        )

    with allure.step("Configure system internal: cert + CA + mTLS + CRL"):
        system.internal.certificate.action_update(primary_cert.name).verify_result()
        system.internal.ca_certificate.action_update(primary_cert.cacert_name).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        system.internal.crl.action_update(crl_name).verify_result()

    with allure.step(f"Configure cluster app {app_name} internal: cert + CA + mTLS + CRL"):
        app.internal.certificate.action_update(primary_cert.name).verify_result()
        app.internal.ca_certificate.action_update(primary_cert.cacert_name).verify_result()
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        app.internal.crl.action_update(crl_name).verify_result()
        wait_for_cluster_app_update(cluster, engines.dut)

    with allure.step("Reset cluster app and verify bridge connection"):
        reset_cluster_app(cluster, app_name, engines.dut)
        verify_nv_bridge_has_connection(engines.dut, expect_connection=True)

    with allure.step("Verify gRPC hello succeeds with active cert"):
        nv_bridge_tool = NvBridgeTool(host=engines.dut.ip)
        nv_bridge_tool.run_bridge_hello(
            client_cert=primary_cert,
            client_cacert=primary_cert,
            plaintext=False,
            expect_success=True,
        )


# ---------------------------------------------------------------------------
# 4.4 – CRL: Revocation negative (revoked cert blocks mTLS / rotation)
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nv_bridge
@pytest.mark.crl
@pytest.mark.usefixtures("enable_cluster", "restore_system_internal_config")
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_bridge_crl_revocation_negative(
    engines,
    devices,
    test_api,
    restore_cluster_app_internal_config,
    import_crl_bridge_certs: BridgeCrlCerts,
    bridge_crl_factory,
):
    """
    Test Objective: Combined negative test.
    Part A: Revoked primary cert blocks mTLS enable on both scopes.
    Part B: Revoked alt cert blocks alt-cert update and rotation.

    Test Flow:
    Part A — Revoked primary blocks mTLS enable:
    1. Generate CRL revoking primary cert
    2. Config system: cert + CA + CRL (succeeds)
    3. Try enable mTLS on system → fails
    4. Config cluster: cert + CA + CRL (succeeds)
    5. Try enable mTLS on cluster → fails
    Part B — Revoked alt blocks update and rotation:
    6. Restore both scopes
    7. Generate CRL revoking alt cert
    8. Config system: valid primary + CA + mTLS + CRL
    9. Try set revoked alt on system → fails
    10. Config cluster: valid primary + CA + mTLS + CRL
    11. Try set revoked alt on cluster → fails
    """
    TestToolkit.tested_api = test_api
    cluster = Cluster()
    system = System()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    primary_cert = import_crl_bridge_certs.primary_cert
    alt_cert = import_crl_bridge_certs.alt_cert

    # Both CRLs share the same CA (bridge_crl certs), so OpenSSL's index.txt
    # is cumulative. Generate alt CRL first so it contains ONLY the alt serial.
    # If primary were revoked first, the alt CRL would also list the primary.
    crl_name_b = "crl_revoked_alt"
    crl_name_a = "crl_revoked_primary"

    with allure.step("Generate CRL revoking alt cert (clean index — alt only)"):
        bridge_crl_factory(
            crl_name=crl_name_b,
            cert_to_revoke=import_crl_bridge_certs.alt_cert,
            certs_dir=import_crl_bridge_certs.certs_dir,
        )

    with allure.step("Generate CRL revoking primary cert"):
        bridge_crl_factory(
            crl_name=crl_name_a,
            cert_to_revoke=import_crl_bridge_certs.primary_cert,
            certs_dir=import_crl_bridge_certs.certs_dir,
        )

    # --- Part A: Revoked primary blocks mTLS enable ---

    with allure.step("Config system: cert + CA + CRL with revoked primary"):
        system.internal.certificate.action_update(primary_cert.name).verify_result()
        system.internal.ca_certificate.action_update(primary_cert.cacert_name).verify_result()
        system.internal.crl.action_update(crl_name_a).verify_result()

    with allure.step("Try enable mTLS on system - expect fail (cert is revoked)"):
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result(should_succeed=False)

    with allure.step(f"Config cluster {app_name}: cert + CA + CRL with revoked primary"):
        app.internal.certificate.action_update(primary_cert.name).verify_result()
        app.internal.ca_certificate.action_update(primary_cert.cacert_name).verify_result()
        app.internal.crl.action_update(crl_name_a).verify_result()

    with allure.step("Try enable mTLS on cluster - expect fail (cert is revoked)"):
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result(should_succeed=False)

    # --- Part B: Revoked alt blocks update and rotation ---

    with allure.step("Part B: Restore both scopes for clean state"):
        system.internal.crl.action_restore().verify_result()
        system.internal.certificate.action_restore().verify_result()
        system.internal.ca_certificate.action_restore().verify_result()
        app.internal.crl.action_restore().verify_result()
        app.internal.certificate.action_restore().verify_result()
        app.internal.ca_certificate.action_restore().verify_result()

    with allure.step("Config system: valid primary + CA + mTLS + CRL"):
        system.internal.certificate.action_update(primary_cert.name).verify_result()
        system.internal.ca_certificate.action_update(primary_cert.cacert_name).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        system.internal.crl.action_update(crl_name_b).verify_result()

    with allure.step("Try set revoked alt on system - expect fail"):
        system.internal.alternate_certificate.action_update(alt_cert.name).verify_result(should_succeed=False)

    with allure.step(f"Config cluster {app_name}: valid primary + CA + mTLS + CRL"):
        app.internal.certificate.action_update(primary_cert.name).verify_result()
        app.internal.ca_certificate.action_update(primary_cert.cacert_name).verify_result()
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        wait_for_cluster_app_update(cluster, engines.dut)
        app.internal.crl.action_update(crl_name_b).verify_result()

    with allure.step("Try set revoked alt on cluster - expect fail"):
        app.internal.alternate_certificate.action_update(alt_cert.name).verify_result(should_succeed=False)


# ---------------------------------------------------------------------------
# 4.5 – CRL: Wrong CA chain blocks CRL update (P2)
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nv_bridge
@pytest.mark.crl
@pytest.mark.usefixtures("enable_cluster", "restore_system_internal_config")
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_bridge_crl_wrong_chain_block_update_negative(
    engines,
    devices,
    test_api,
    restore_cluster_app_internal_config,
    import_crl_bridge_certs: BridgeCrlCerts,
    import_certs_with_alt: tuple[CertInfo, CertInfo, CertInfo],
    bridge_crl_factory,
):
    """
    Test Objective: CRL from a different CA chain is rejected when bound to
    system or cluster internal scope.

    Test Flow:
    1. Configure system: cert + CA (chain A from CRL certs) + mTLS
    2. Generate CRL from chain B (import_certs_with_alt CA)
    3. Try bind wrong-chain CRL to system → fails
    4. Configure cluster: cert + CA (chain A) + mTLS
    5. Try bind wrong-chain CRL to cluster → fails
    """
    TestToolkit.tested_api = test_api
    cluster = Cluster()
    system = System()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    chain_a_cert = import_crl_bridge_certs.primary_cert
    chain_b_cert, _, _ = import_certs_with_alt
    wrong_chain_crl_name = "wrong_chain_crl"

    with allure.step("Configure system: cert + CA (chain A) + mTLS"):
        system.internal.certificate.action_update(chain_a_cert.name).verify_result()
        system.internal.ca_certificate.action_update(chain_a_cert.cacert_name).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()

    with allure.step("Generate CRL from chain B"):
        # certs_dir intentionally omitted: factory derives it from chain_b_cert
        # (a different CA chain than chain_a_cert).
        bridge_crl_factory(
            crl_name=wrong_chain_crl_name,
            cert_to_revoke=chain_b_cert,
        )

    with allure.step("Try bind wrong-chain CRL to system - expect fail"):
        system.internal.crl.action_update(wrong_chain_crl_name).verify_result(should_succeed=False)

    with allure.step(f"Configure cluster {app_name}: cert + CA (chain A) + mTLS"):
        app.internal.certificate.action_update(chain_a_cert.name).verify_result()
        app.internal.ca_certificate.action_update(chain_a_cert.cacert_name).verify_result()
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        wait_for_cluster_app_update(cluster, engines.dut)

    with allure.step("Try bind wrong-chain CRL to cluster - expect fail"):
        app.internal.crl.action_update(wrong_chain_crl_name).verify_result(should_succeed=False)


# ---------------------------------------------------------------------------
# 4.6 – CRL: Restore + reset connections after CRL removal (P2)
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nv_bridge
@pytest.mark.crl
@pytest.mark.usefixtures("enable_cluster", "restore_system_internal_config")
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_bridge_restore_reset_cleanup_crl_positive(
    engines,
    devices,
    test_api,
    restore_cluster_app_internal_config,
    import_crl_bridge_certs: BridgeCrlCerts,
    bridge_crl_factory,
):
    """
    Test Objective: Restore CRL clears show output. Reset connections works
    gracefully after CRL removal on both system and cluster scopes.

    Test Flow:
    1. Configure system: cert + CA + mTLS + CRL
    2. Verify CRL in system show
    3. Restore system CRL → verify cleared
    4. Configure cluster: cert + CA + mTLS + CRL
    5. Verify CRL in cluster show
    6. Restore cluster CRL → verify cleared
    7. Reset cluster connections → no error
    """
    TestToolkit.tested_api = test_api
    cluster = Cluster()
    system = System()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    primary_cert = import_crl_bridge_certs.primary_cert
    crl_name = "restore_cleanup_crl"

    with allure.step("Generate CRL revoking unrelated cert"):
        bridge_crl_factory(
            crl_name=crl_name,
            cert_to_revoke=import_crl_bridge_certs.revoked_primary_cert,
            certs_dir=import_crl_bridge_certs.certs_dir,
        )

    # --- System scope ---

    with allure.step("Configure system: cert + CA + mTLS + CRL"):
        system.internal.certificate.action_update(primary_cert.name).verify_result()
        system.internal.ca_certificate.action_update(primary_cert.cacert_name).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        system.internal.crl.action_update(crl_name).verify_result()

    with allure.step("Verify CRL appears in system show"):
        verify_system_internal_crl_show(expect_crl=crl_name)

    with allure.step("Restore system CRL"):
        system.internal.crl.action_restore().verify_result()

    with allure.step("Verify CRL is cleared in system show"):
        verify_system_internal_crl_show(expect_crl="")

    # --- Cluster scope ---

    with allure.step(f"Configure cluster {app_name}: cert + CA + mTLS + CRL"):
        app.internal.certificate.action_update(primary_cert.name).verify_result()
        app.internal.ca_certificate.action_update(primary_cert.cacert_name).verify_result()
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        wait_for_cluster_app_update(cluster, engines.dut)
        app.internal.crl.action_update(crl_name).verify_result()

    with allure.step("Verify CRL appears in cluster show"):
        verify_cluster_app_internal_crl_show(app_name, expect_crl=crl_name)

    with allure.step("Restore cluster CRL"):
        app.internal.crl.action_restore().verify_result()

    with allure.step("Verify CRL is cleared in cluster show"):
        verify_cluster_app_internal_crl_show(app_name, expect_crl="")

    with allure.step("Reset cluster connections after CRL removal"):
        reset_cluster_app(cluster, app_name, engines.dut)


# ---------------------------------------------------------------------------
# 4.7 – SPIFFE: Positive flow (JSON extraction + functional connection)
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nv_bridge
@pytest.mark.spiffe
@pytest.mark.usefixtures("enable_cluster", "restore_system_internal_config")
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
@pytest.mark.parametrize(
    "generate_spiffe_bridge_certs",
    ["same_spiffe_pair", "different_spiffe_pair"],
    indirect=True,
)
def test_bridge_spiffe_positive_flow(
    engines,
    devices,
    test_api,
    restore_cluster_app_internal_config,
    import_spiffe_bridge_certs: BridgeSpiffeCerts,
):
    """
    Test Objective: End-to-end SPIFFE positive test parametrized across
    multiple SPIFFE configurations.

    SPIFFE extraction works on both the primary cert and the alternate cert
    independently. At least one must carry a SPIFFE SAN URI. The primary and
    alternate certs do NOT need to have matching SPIFFE URIs.

    Variants:
    - same_spiffe_pair:      both certs carry the same SPIFFE URI
    - different_spiffe_pair: certs carry different SPIFFE URIs (both valid)

    Test Flow:
    Part A — SPIFFE extraction:
    1. Verify system JSON has no SPIFFE initially
    2. Update system primary cert → verify cert SPIFFE in JSON
    3. Update system alt-cert → verify alt SPIFFE in JSON
    4. Repeat for cluster scope
    Part B — Functional connection:
    5. Set CA + enable mTLS on both scopes
    6. Reset cluster app → verify connection
    7. Reset system connections → verify connection
    """
    TestToolkit.tested_api = test_api
    cluster = Cluster()
    system = System()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    spiffe_certs = import_spiffe_bridge_certs
    params = spiffe_certs.params

    # --- Part A: SPIFFE extraction to JSON ---

    with allure.step("Verify system JSON has no SPIFFE initially"):
        verify_system_internal_spiffe_json(
            engines.dut,
            expected_cert_spiffe="",
            expected_alt_cert_spiffe="",
        )

    with allure.step("Update system primary cert and verify SPIFFE JSON"):
        system.internal.certificate.action_update(
            spiffe_certs.system_primary_cert.name,
        ).verify_result()
        verify_system_internal_spiffe_json(
            engines.dut,
            expected_cert_spiffe=params.system_primary_spiffe,
            expected_alt_cert_spiffe="",
        )

    with allure.step("Update system alt-cert and verify SPIFFE JSON"):
        system.internal.alternate_certificate.action_update(
            spiffe_certs.system_alt_cert.name,
        ).verify_result()
        verify_system_internal_spiffe_json(
            engines.dut,
            expected_cert_spiffe=params.system_primary_spiffe,
            expected_alt_cert_spiffe=params.alternate_cert_spiffe,
        )

    with allure.step("Verify cluster JSON has no SPIFFE initially"):
        verify_cluster_app_internal_spiffe_json(
            engines.dut,
            app_name,
            expected_cert_spiffe="",
            expected_alt_cert_spiffe="",
        )

    with allure.step(f"Update cluster {app_name} primary cert and verify SPIFFE JSON"):
        app.internal.certificate.action_update(
            spiffe_certs.cluster_primary_cert.name,
        ).verify_result()
        verify_cluster_app_internal_spiffe_json(
            engines.dut,
            app_name,
            expected_cert_spiffe=params.cluster_primary_spiffe,
            expected_alt_cert_spiffe="",
        )

    with allure.step(f"Update cluster {app_name} alt-cert and verify SPIFFE JSON"):
        app.internal.alternate_certificate.action_update(
            spiffe_certs.cluster_alt_cert.name,
        ).verify_result()
        verify_cluster_app_internal_spiffe_json(
            engines.dut,
            app_name,
            expected_cert_spiffe=params.cluster_primary_spiffe,
            expected_alt_cert_spiffe=params.alternate_cert_spiffe,
        )

    # --- Part B: Functional connection ---

    with allure.step("Set CA + enable mTLS on system"):
        system.internal.ca_certificate.action_update(
            spiffe_certs.system_primary_cert.cacert_name,
        ).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()

    with allure.step(f"Set CA + enable mTLS on cluster {app_name}"):
        app.internal.ca_certificate.action_update(
            spiffe_certs.cluster_primary_cert.cacert_name,
        ).verify_result()
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        wait_for_cluster_app_update(cluster, engines.dut)

    with allure.step("Reset cluster app and verify bridge connection"):
        reset_cluster_app(cluster, app_name, engines.dut)
        verify_nv_bridge_has_connection(engines.dut, expect_connection=True)


# ---------------------------------------------------------------------------
# 4.7b – SPIFFE: Partial URI coverage negative (P2)
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nv_bridge
@pytest.mark.spiffe
@pytest.mark.usefixtures("enable_cluster", "restore_system_internal_config")
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
@pytest.mark.parametrize(
    "generate_spiffe_bridge_certs",
    ["cert_only_spiffe", "alt_only_spiffe"],
    indirect=True,
)
def test_bridge_spiffe_partial_uri_negative(
    engines,
    devices,
    test_api,
    restore_cluster_app_internal_config,
    import_spiffe_bridge_certs: BridgeSpiffeCerts,
):
    """
    Test Objective: Verify that mTLS enable is rejected when only one of the
    primary/alternate certs carries a SPIFFE URI.

    Variants:
    - cert_only_spiffe: only the primary cert has a SPIFFE URI; alt is empty
    - alt_only_spiffe:  only the alternate cert has a SPIFFE URI; primary is empty

    Test Flow:
    1. Update system and cluster primary + alt certs
    2. Verify SPIFFE JSON extraction (empty where cert lacks SPIFFE URI)
    3. Set CA on both scopes
    4. Try to enable mTLS — expect rejection due to partial SPIFFE coverage
    """
    TestToolkit.tested_api = test_api
    cluster = Cluster()
    system = System()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    spiffe_certs = import_spiffe_bridge_certs
    params = spiffe_certs.params

    with allure.step("Update system primary and alt certs"):
        system.internal.certificate.action_update(
            spiffe_certs.system_primary_cert.name,
        ).verify_result()
        system.internal.alternate_certificate.action_update(
            spiffe_certs.system_alt_cert.name,
        ).verify_result()

    with allure.step("Verify system SPIFFE JSON extraction"):
        verify_system_internal_spiffe_json(
            engines.dut,
            expected_cert_spiffe=params.system_primary_spiffe,
            expected_alt_cert_spiffe=params.alternate_cert_spiffe,
        )

    with allure.step(f"Update cluster {app_name} primary and alt certs"):
        app.internal.certificate.action_update(
            spiffe_certs.cluster_primary_cert.name,
        ).verify_result()
        app.internal.alternate_certificate.action_update(
            spiffe_certs.cluster_alt_cert.name,
        ).verify_result()

    with allure.step(f"Verify cluster {app_name} SPIFFE JSON extraction"):
        verify_cluster_app_internal_spiffe_json(
            engines.dut,
            app_name,
            expected_cert_spiffe=params.cluster_primary_spiffe,
            expected_alt_cert_spiffe=params.alternate_cert_spiffe,
        )

    with allure.step("Set CA on both scopes"):
        system.internal.ca_certificate.action_update(
            spiffe_certs.system_primary_cert.cacert_name,
        ).verify_result()
        app.internal.ca_certificate.action_update(
            spiffe_certs.cluster_primary_cert.cacert_name,
        ).verify_result()

    with allure.step("Enable mTLS on system - expect rejection (partial SPIFFE)"):
        system.internal.encryption.action_update(
            EncryptionMode.MTLS,
        ).verify_result(should_succeed=False)

    with allure.step(f"Enable mTLS on cluster {app_name} - expect rejection (partial SPIFFE)"):
        app.internal.encryption.action_update(
            EncryptionMode.MTLS,
        ).verify_result(should_succeed=False)


# ---------------------------------------------------------------------------
# 4.8 – SPIFFE: Cluster-system URI mismatch negative (P2)
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nv_bridge
@pytest.mark.spiffe
@pytest.mark.usefixtures("enable_cluster", "restore_system_internal_config")
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
@pytest.mark.parametrize("generate_spiffe_bridge_certs", ["cluster_system_primary_mismatch"], indirect=True)
def test_bridge_spiffe_cluster_system_uri_mismatch_negative(
    engines,
    devices,
    test_api,
    restore_cluster_app_internal_config,
    import_spiffe_bridge_certs: BridgeSpiffeCerts,
):
    """
    Test Objective: System uses a different SPIFFE URI than cluster, so the
    bridge connection should fail.

    Test Flow:
    1. Configure system: cert (spiffe://system) + CA + mTLS
    2. Configure cluster: cert (spiffe://cluster) + CA + mTLS
    3. Verify SPIFFE URIs differ between scopes
    4. Reset cluster app and verify connection fails
    """
    TestToolkit.tested_api = test_api
    cluster = Cluster()
    system = System()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    spiffe_certs = import_spiffe_bridge_certs
    params = spiffe_certs.params

    with allure.step("Configure system: cert + CA + mTLS"):
        system.internal.certificate.action_update(
            spiffe_certs.system_primary_cert.name,
        ).verify_result()
        system.internal.ca_certificate.action_update(
            spiffe_certs.system_primary_cert.cacert_name,
        ).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()

    with allure.step(f"Configure cluster {app_name}: cert + CA + mTLS"):
        app.internal.certificate.action_update(
            spiffe_certs.cluster_primary_cert.name,
        ).verify_result()
        app.internal.ca_certificate.action_update(
            spiffe_certs.cluster_primary_cert.cacert_name,
        ).verify_result()
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        wait_for_cluster_app_update(cluster, engines.dut)

    with allure.step("Verify SPIFFE URIs differ"):
        verify_system_internal_spiffe_json(
            engines.dut,
            expected_cert_spiffe=params.system_primary_spiffe,
        )
        verify_cluster_app_internal_spiffe_json(
            engines.dut,
            app_name,
            expected_cert_spiffe=params.cluster_primary_spiffe,
        )
        assert params.system_primary_spiffe != params.cluster_primary_spiffe, (
            "Test requires different SPIFFE URIs between system and cluster"
        )

    with allure.step("Reset cluster app and verify connection fails"):
        reset_cluster_app(cluster, app_name, engines.dut)
        verify_nv_bridge_has_connection(engines.dut, expect_connection=False)


# ---------------------------------------------------------------------------
# 4.9 – Reboot persistence: CRL + SPIFFE + mTLS survives reboot
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.security
@pytest.mark.nv_bridge
@pytest.mark.crl
@pytest.mark.spiffe
@pytest.mark.reboot
@pytest.mark.track_serial_console
@pytest.mark.usefixtures("enable_cluster", "restore_system_internal_config")
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
@pytest.mark.parametrize("generate_spiffe_bridge_certs", ["same_spiffe_pair"], indirect=True)
def test_bridge_upgrade_reboot_persistence_crl_spiffe(
    engines,
    devices,
    test_api,
    restore_cluster_app_internal_config,
    import_spiffe_bridge_certs: BridgeSpiffeCerts,
    bridge_crl_factory,
):
    """
    Test Objective: Full config (SPIFFE certs + CRL + mTLS) survives a system
    DUT reboot. Both scopes are configured, but only the system DUT is saved
    and rebooted; cluster config is re-verified for consistency.

    Test Flow:
    1. Configure system: SPIFFE cert + CA + mTLS + CRL
    2. Configure cluster: SPIFFE cert + CA + mTLS + CRL
    3. Verify all config pre-reboot
    4. Save config and reboot system DUT
    5. Verify all config persisted after reboot
    6. Verify bridge connection after reboot
    """
    TestToolkit.tested_api = test_api
    cluster = Cluster()
    system = System()
    app_name = restore_cluster_app_internal_config
    app = cluster.apps.app_name[app_name]
    spiffe_certs = import_spiffe_bridge_certs
    params = spiffe_certs.params
    crl_name = "reboot_persist_crl"

    with allure.step("Generate CRL revoking unrelated cert from the same CA"):
        bridge_crl_factory(
            crl_name=crl_name,
            cert_to_revoke=spiffe_certs.system_alt_cert,
            certs_dir=spiffe_certs.certs_dir,
        )

    with allure.step("Configure system: SPIFFE cert + CA + mTLS + CRL"):
        system.internal.certificate.action_update(
            spiffe_certs.system_primary_cert.name,
        ).verify_result()
        system.internal.ca_certificate.action_update(
            spiffe_certs.system_primary_cert.cacert_name,
        ).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        system.internal.crl.action_update(crl_name).verify_result()

    with allure.step(f"Configure cluster {app_name}: SPIFFE cert + CA + mTLS + CRL"):
        app.internal.certificate.action_update(
            spiffe_certs.cluster_primary_cert.name,
        ).verify_result()
        app.internal.ca_certificate.action_update(
            spiffe_certs.cluster_primary_cert.cacert_name,
        ).verify_result()
        app.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        wait_for_cluster_app_update(cluster, engines.dut)
        app.internal.crl.action_update(crl_name).verify_result()

    with allure.step("Verify all config pre-reboot"):
        verify_system_internal_show(
            expect_cert=spiffe_certs.system_primary_cert.name,
            expect_cacert=spiffe_certs.system_primary_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
            expect_crl=crl_name,
        )
        verify_system_internal_crl_show(expect_crl=crl_name)
        verify_system_internal_spiffe_json(
            engines.dut,
            expected_cert_spiffe=params.system_primary_spiffe,
        )
        verify_cluster_app_internal_show(
            app_name,
            expect_cert=spiffe_certs.cluster_primary_cert.name,
            expect_cacert=spiffe_certs.cluster_primary_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
            expect_crl=crl_name,
        )
        verify_cluster_app_internal_crl_show(app_name, expect_crl=crl_name)
        verify_cluster_app_internal_spiffe_json(
            engines.dut,
            app_name,
            expected_cert_spiffe=params.cluster_primary_spiffe,
        )

    with allure.step("Save config and reboot DUT"):
        save_config_and_reboot(engines)

    with allure.step("Wait for cluster readiness after reboot"):
        wait_for_cluster_app_update(cluster, engines.dut)

    with allure.step("Verify all config persisted after reboot"):
        verify_system_internal_show(
            expect_cert=spiffe_certs.system_primary_cert.name,
            expect_cacert=spiffe_certs.system_primary_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
            expect_crl=crl_name,
        )
        verify_system_internal_crl_show(expect_crl=crl_name)
        verify_system_internal_spiffe_json(
            engines.dut,
            expected_cert_spiffe=params.system_primary_spiffe,
        )
        verify_cluster_app_internal_show(
            app_name,
            expect_cert=spiffe_certs.cluster_primary_cert.name,
            expect_cacert=spiffe_certs.cluster_primary_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
            expect_crl=crl_name,
        )
        verify_cluster_app_internal_crl_show(app_name, expect_crl=crl_name)
        verify_cluster_app_internal_spiffe_json(
            engines.dut,
            app_name,
            expected_cert_spiffe=params.cluster_primary_spiffe,
        )

    with allure.step("Verify bridge connection after reboot"):
        verify_nv_bridge_has_connection(engines.dut, expect_connection=True)
