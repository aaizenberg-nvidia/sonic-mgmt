"""UDS Network Isolation — verification tests (TC-DNI-01 through TC-DNI-05).

Covers:
  TC-DNI-01  UDS: monitor user cannot write (enable cluster; on NVLink enable NMX-C /
    NMX-T app managers so component UDS e.g. nmx-t/control.sock exist; gnmic STREAM
    subscribe to keep nv-umf proxy.sock bound; then UDS permission checks)
  TC-DNI-02  Internal ports not exposed (listener check + active probes)
  TC-DNI-03  Published APIs still work (positive flow)
  TC-DNI-05  External port scan with ACL removed

TC-DNI-04 (regression suites) is intentionally excluded — those are separate
pytest invocations listed in the test plan.

Note on TestToolkit.tested_api:
    These tests verify infrastructure-level properties (socket permissions, TCP
    port exposure, iptables filtering) and do not exercise the NVUE / OpenAPI
    layer directly.  Therefore TestToolkit.tested_api is intentionally not set.
"""
import logging
import time
from typing import Dict, List

import pytest

from infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from ngts.nvos_tools.infra.NvBridgeTool import NvBridgeConsts, NvBridgeTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.general.security.nv_bridge.helpers import wait_for_cluster_app_update
from ngts.tests_nvos.general.security.nmx_cert import helpers as nmx_helpers
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import GnmiMode
from ngts.nvos_constants.constants_nvos import ClusterApps
from ngts.constants.constants import GnmiConsts
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.ngts_types import EnginesT

from ngts.tests_nvos.general.security.uds_network_isolation import (
    constants as uds_constants,
    helpers,
)

logger = logging.getLogger(__name__)

# TC-DNI-01: cluster + gnmic STREAM subscribe keep nv-umf/nv-gnmi paths (including
# proxy.sock) materialized; see constants.NV_UMF_PATHS and TC_DNI01_GNMI_SUBSCRIBE_INTERFACE.
UDS_PATH_POLL_INTERVAL_SEC = 5
UDS_PATH_WAIT_MAX_SEC = 1 * 60

_NV_UMF_PATH_SET = frozenset(uds_constants.NV_UMF_PATHS.uds_paths)


# ═══════════════════════════════════════════════════════════════════════════════
# TC-DNI-01 — UDS: monitor user cannot write
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_uds_monitor_cannot_write(
    engines: EnginesT,
    devices,
    is_xdr: bool,
    monitor_engine: LinuxSshEngine,
    uds_paths: List[str],
    enable_cluster,
    enable_app_manager,
):
    """TC-DNI-01: verify the monitor user has no write permission on any
    host-mounted Unix domain socket (owner/mode are logged but not asserted)."""
    check_nv_umf_paths = bool(_NV_UMF_PATH_SET.intersection(uds_paths))
    cluster = enable_cluster
    dut = engines.dut
    dut_ip = engines.dut.ip
    gnmi_port = GnmiConsts.GNMI_DEFAULT_PORT
    logger.info(
        "TC-DNI-01 UDS wait with cluster + gnmic subscribe for %s (is_xdr=%s)",
        devices.dut,
        is_xdr,
    )

    if not is_xdr:
        # Per-app managers default to disabled on many images; nmx-c / nmx-t UDS
        # (e.g. /var/run/nmx-t/control.sock) are not bound until managers are enabled.
        # `enable_app_manager` registers each manager for restore on teardown so
        # state does not leak into subsequent tests.
        with allure.step("Enable NMX-C and NMX-T cluster app managers for UDS paths"):
            for app_name, label in (
                (ClusterApps.NMX_CONTROLLER, "NMX-C"),
                (ClusterApps.NMX_TELEMETRY, "NMX-T"),
            ):
                with allure.step(f"Enable {label} manager"):
                    enable_app_manager(cluster.apps.app_name[app_name].manager)

    client = None
    gnmic_proc = None
    try:
        if check_nv_umf_paths:
            client = GnmiClient(
                server_host=dut_ip,
                server_port=gnmi_port,
                username=devices.dut.default_username,
                password=devices.dut.default_password,
                verify_tools_installed=True,
            )
            with allure.step(
                "Start gnmic STREAM subscribe (nvos target) to bind nv-umf proxy path chain"
            ):
                gnmic_proc = client.gnmic_subscribe_interface_and_keep_session_alive(
                    GnmiMode.STREAM,
                    uds_constants.TC_DNI01_GNMI_SUBSCRIBE_INTERFACE,
                    skip_cert_verify=True,
                    debug_mode=False,
                )

        with allure.step(
            f"Wait up to {UDS_PATH_WAIT_MAX_SEC}s for all UDS paths to exist "
            f"(poll every {UDS_PATH_POLL_INTERVAL_SEC}s)"
        ):
            deadline = time.time() + UDS_PATH_WAIT_MAX_SEC
            last_missing: List[str] = []
            while time.time() < deadline:
                last_missing = [
                    p for p in uds_paths if not helpers.check_socket_exists(dut, p)
                ]
                if not last_missing:
                    logger.info("All %d configured UDS path(s) are present", len(uds_paths))
                    break
                logger.info(
                    "Waiting for %d missing UDS path(s) (next check in %ds): %s",
                    len(last_missing),
                    UDS_PATH_POLL_INTERVAL_SEC,
                    last_missing,
                )
                time.sleep(UDS_PATH_POLL_INTERVAL_SEC)
            assert not last_missing, (
                f"After {UDS_PATH_WAIT_MAX_SEC}s, UDS socket paths still missing on DUT: "
                f"{last_missing}"
            )

        with allure.step("Verify monitor user cannot write to any UDS"):
            for path in uds_paths:
                owner, perms = helpers.get_file_owner_and_perms(dut, path)
                logger.info(
                    "UDS path %s: DUT owner=%s permissions=%s (verify monitor cannot write)",
                    path,
                    owner,
                    perms,
                )
                assert not helpers.check_can_write(monitor_engine, path), (
                    f"SECURITY: monitor user CAN write to {path}"
                )
    finally:
        if gnmic_proc is not None and client is not None:
            with allure.step("Stop gnmic subscribe session"):
                client.close_session_and_get_out_and_err(gnmic_proc)


# ═══════════════════════════════════════════════════════════════════════════════
# TC-DNI-02 — Internal ports not exposed (listener check + active probes)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
@pytest.mark.timeout(3 * MINUTE, func_only=True)
def test_internal_ports_not_exposed(
    engines: EnginesT,
    internal_probe_ports: Dict[int, str],
):
    """TC-DNI-02: validate internal ports are unreachable from the host."""
    if not internal_probe_ports:
        pytest.skip("No internal probe ports for this setup (XDR)")

    dut = engines.dut

    with allure.step("Collect TCP/UDP listeners on host"):
        ss_output = helpers.run_ss_tulpn(dut)
        allure.attach("ss -tulpn output", ss_output)
        listeners = helpers.parse_ss_listeners(ss_output)
        listening_ports = helpers.extract_listening_ports(listeners)
        logger.info("Listening ports: %s", sorted(listening_ports))

    with allure.step("Verify internal ports are not in listener list"):
        listener_violations: List[str] = []
        for port, label in internal_probe_ports.items():
            matching = [e for e in listeners if e.port == port]
            if matching:
                lines = [e.raw_line for e in matching]
                listener_violations.append(
                    f"port {port} ({label}): {lines}"
                )
        assert not listener_violations, (
            "Internal port(s) found listening on host (ss):\n" +
            "\n".join(listener_violations)
        )

    with allure.step("Active TCP probe to each internal port"):
        probe_violations: List[str] = []
        for port, label in internal_probe_ports.items():
            is_open = helpers.tcp_probe(dut, "127.0.0.1", port)
            if is_open:
                probe_violations.append(f"port {port} ({label}) accepted a TCP connection")
            else:
                logger.info("Confirmed: port %d (%s) refused/timed out", port, label)
        assert not probe_violations, (
            "Internal port(s) accepted TCP from localhost:\n" +
            "\n".join(probe_violations)
        )


# ═══════════════════════════════════════════════════════════════════════════════
# TC-DNI-03 — Published APIs still work (positive flow)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
@pytest.mark.timeout(3 * MINUTE, func_only=True)
@pytest.mark.parametrize(
    "app_name, label",
    [
        (ClusterApps.NMX_CONTROLLER, "NMX-C"),
        (ClusterApps.NMX_TELEMETRY, "NMX-T"),
    ],
    ids=["nmx_c", "nmx_t"],
)
def test_grpc_hello(engines: EnginesT, is_xdr: bool, enable_cluster, enable_app_manager, app_name, label: str):
    """TC-DNI-03: positive control — NMX manager gRPC hello."""
    # The `enable_cluster` fixture only brings the cluster container up; per-app
    # managers default to state=disabled, so the external gRPC port has no
    # listener (e.g. NMX-C port 9370). We must explicitly enable the per-app
    # manager before the gRPC call. `enable_app_manager` registers the manager
    # for restore (action_restore) on teardown so state does not leak into
    # subsequent tests.
    if is_xdr:
        pytest.skip(f"{label} not available on XDR setups")

    cluster = enable_cluster
    manager = cluster.apps.app_name[app_name].manager

    with allure.step(f"Enable {label} app manager"):
        enable_app_manager(manager)

    with allure.step(f"gRPC hello to {label} manager"):
        dut_ip = engines.dut.ip
        # Cert params are required by the API but ignored when TLS is disabled.
        cert_info = CertInfo(name="dni-test", ip=dut_ip)
        result = nmx_helpers.run_manager_hello_request(
            app_name=app_name,
            client_tls_mode="disabled",
            server_cert=cert_info,
            server_ca=cert_info,
            client_cert=cert_info,
            client_ca=cert_info,
        )
        result.verify_result()


@pytest.mark.security
@pytest.mark.timeout(3 * MINUTE, func_only=True)
def test_gnmi_get(engines: EnginesT, devices):
    """TC-DNI-03: gNMI Get / capabilities sanity."""
    with allure.step("gNMI Get request to verify gNMI server is functional"):
        dut_ip = engines.dut.ip
        gnmi_port = GnmiConsts.GNMI_DEFAULT_PORT
        # verify_tools_installed=True ensures `gnmic` is installed on the
        # local sonic-mgmt runner before any subprocess invocation.
        client = GnmiClient(
            server_host=dut_ip,
            server_port=gnmi_port,
            username=devices.dut.default_username,
            password=devices.dut.default_password,
            verify_tools_installed=True,
        )
        out, err = client.gnmic_capabilities(skip_cert_verify=True)
        assert out and "not found" not in out.lower(), (
            f"gNMI capabilities request failed: stdout={out}, stderr={err}"
        )


@pytest.mark.security
@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_nv_bridge_hello(
    engines: EnginesT,
    is_xdr: bool,
    enable_cluster,
    enable_app_manager,
    verify_grpc_tools_installed,
):
    """TC-DNI-03: NV-Bridge hello positive path."""
    if is_xdr:
        pytest.skip("NV-Bridge not available on XDR setups")

    # Same pattern as test_bridge_main_flow (nv_bridge_encryption): fresh Cluster()
    # API object after the fixture has brought the cluster up (no cert / mtls here).
    _ = enable_cluster
    cluster = Cluster()
    manager = cluster.apps.app_name[ClusterApps.NMX_CONTROLLER].manager

    with allure.step("Enable NMX-C app manager (NV-Bridge Hello backend)"):
        enable_app_manager(manager)

    with allure.step("Wait for cluster app (NMX-C up) before NV-Bridge Hello"):
        wait_for_cluster_app_update(cluster, engines.dut)

    # Do not gate on `nv show nv-bridge` connections: that map can stay empty
    # until after the first external Hello or while the bridge is still
    # attaching, which makes verify_nv_bridge_has_connection a false failure.

    nv_bridge_tool = NvBridgeTool(host=engines.dut.ip)
    with allure.step("Wait for NV-Bridge to be ready to answer Hello (plaintext)"):
        ready = nv_bridge_tool.wait_for_hello_ready(plaintext=True)
        assert ready, (
            f"NV-Bridge did not accept a plaintext Hello within "
            f"{NvBridgeConsts.HELLO_READY_TIMEOUT_SEC}s on "
            f"{engines.dut.ip}:{nv_bridge_tool.port}."
        )

    with allure.step("Verify basic grpc hello request to bridge (plaintext)"):
        nv_bridge_tool.run_bridge_hello(plaintext=True, expect_success=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TC-DNI-05 — External port scan with ACL removed
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
@pytest.mark.timeout(20 * MINUTE, func_only=True)
def test_external_port_scan_with_acl_removed(
    engines: EnginesT,
    scanner_engine: LinuxSshEngine,
    published_ports: Dict[int, str],
    internal_probe_ports: Dict[int, str],
):
    """TC-DNI-05: remove firewall, run nmap from an external host, verify
    only published ports are open."""
    dut = engines.dut
    dut_ip = dut.ip

    with allure.step("Save current iptables rules"):
        helpers.save_iptables(dut)

    try:
        with allure.step("Flush iptables (remove ACL)"):
            helpers.flush_iptables(dut)

        with allure.step(f"Run nmap from external host against {dut_ip}"):
            nmap_output = helpers.run_nmap(scanner_engine, dut_ip)
            allure.attach("nmap output", nmap_output)
            open_ports = helpers.parse_nmap_open_ports(nmap_output)
            logger.info("nmap open ports: %s", sorted(open_ports))

        with allure.step("Compare open ports against published-port allowlist"):
            allowed = set(published_ports.keys())
            allowed.add(uds_constants.SSH_PORT)
            unexpected = open_ports - allowed
            assert not unexpected, (
                f"Unexpected ports open: {sorted(unexpected)}. "
                f"Allowed: {sorted(allowed)}"
            )

        with allure.step("Verify internal ports are not in nmap results"):
            for port, label in internal_probe_ports.items():
                assert port not in open_ports, (
                    f"Internal port {port} ({label}) found open in nmap scan"
                )

    finally:
        with allure.step("Restore iptables rules"):
            helpers.restore_iptables(dut)
