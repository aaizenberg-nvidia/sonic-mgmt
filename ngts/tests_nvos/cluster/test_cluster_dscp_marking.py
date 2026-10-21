"""
Test DSCP Marking Configuration for Cluster Control Plane

Validates DSCP marking configuration for VR-NVL144 switch tray clusters.
Requires 2 DUT switches with NVOS/NVUE and tcpdump installed.
Traffic: nv-bridge port 50052, DSCP marking on inter-node gRPC.
"""
import logging
import random
import shlex
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import pytest

from ngts.cli_wrappers.openapi.openapi_command_builder import OpenApiRequest
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.PacketCaptureTool import NetworkInterfaceUtils, PacketCaptureTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.cluster.dscp_marking_tools import (
    ClusterConfigHelper,
    ControlPlaneTrafficTools,
    DscpCaptureVerifier,
    DscpMarkingConstants,
    DscpMarkingTestFacade,
    DscpValueSelector,
)
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


# Error keywords for validating rejection of invalid DSCP values
ERROR_KEYWORDS = ['error', 'invalid', 'incomplete', 'not one of', 'not an integer', 'valid range']

# Invalid DSCP values for parameterized testing
INVALID_DSCP_TEST_VALUES = [
    pytest.param(-1, "Below minimum", id="below_min"),
    pytest.param(64, "Above maximum", id="above_max"),
    pytest.param('abc', "Invalid enum", id="invalid_enum"),
    pytest.param('@#$', "Special chars", id="special_chars"),
    pytest.param('46.5', "Float value", id="float_value"),
    pytest.param('', "Empty value", id="empty_value"),
]


def verify_invalid_dscp_rejected(engines, invalid_value: Any, description: str) -> None:
    """Verify that an invalid DSCP value is rejected by the CLI."""
    cmd = f"nv set cluster dscp-marking {shlex.quote(str(invalid_value))}"
    output = engines.dut.run_cmd(cmd, validate=False)
    output_lower = output.lower() if output else ""

    rejected = any(kw in output_lower for kw in ERROR_KEYWORDS)
    assert rejected, (
        f"Invalid DSCP value '{invalid_value}' ({description}) should be rejected. "
        f"Output: {output}"
    )


class IpVersion(Enum):
    """IP version for traffic capture."""
    IPV4 = ("IPv4", False, "/tmp/nv_bridge_dscp_node{idx}.pcap")
    IPV6 = ("IPv6", True, "/tmp/nv_bridge_dscp_node{idx}_v6.pcap")

    def __init__(self, label: str, is_ipv6: bool, capture_file_template: str):
        self.label = label
        self.is_ipv6 = is_ipv6
        self.capture_file_template = capture_file_template

    def get_capture_file(self, idx: int) -> str:
        return self.capture_file_template.format(idx=idx)


@dataclass
class TrafficVerificationResult:
    """Result of DSCP verification for a single node."""
    ip: str
    found: bool
    packets: int
    snippet: str


@dataclass
class DscpVerificationContext:
    """Context for DSCP verification across nodes."""
    engines_list: List[Any]
    interface: str
    dscp_numeric: int
    ip_version: IpVersion
    cluster: Cluster  # Cluster object for OOP operations (type-safe)
    capture_duration_seconds: int  # Dynamic duration so capture does not end before traffic
    capture_files: Dict[str, str] = field(default_factory=dict)


@dataclass
class ClusterIpVersionConfig:
    """Configuration for cluster with specific IP version addresses."""
    ip_version: IpVersion
    node_ips: List[str]  # IP addresses to use for cluster node configuration

    @property
    def label(self) -> str:
        return self.ip_version.label


def _get_node_ipv6_addresses(engines_list: List[Any], interface: str) -> Optional[List[str]]:
    """Get IPv6 addresses for all cluster nodes. Returns None if any node lacks IPv6."""
    ipv6_addresses = []

    for engine in engines_list:
        try:
            ipv6 = NetworkInterfaceUtils.get_interface_ipv6(engine, interface)
            if ipv6:
                ipv6_addresses.append(ipv6)
                logger.info(f"Node {engine.ip}: IPv6 address on {interface} = {ipv6}")
            else:
                logger.info(f"Node {engine.ip}: No global IPv6 address on {interface}")
                return None
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.warning(f"Connection error getting IPv6 for {engine.ip}: {e}")
            return None
        except (AttributeError, ValueError) as e:
            logger.warning(f"Data error getting IPv6 for {engine.ip}: {e}")
            return None

    return ipv6_addresses if len(ipv6_addresses) == len(engines_list) else None


def _get_cluster_ip_configs(engines_list: List[Any], interface: str) -> List[ClusterIpVersionConfig]:
    """Get cluster IP configs for IPv4 (always) and IPv6 (if available)."""
    configs = [ClusterIpVersionConfig(IpVersion.IPV4, [e.ip for e in engines_list])]

    ipv6_ips = _get_node_ipv6_addresses(engines_list, interface)
    if ipv6_ips:
        configs.append(ClusterIpVersionConfig(IpVersion.IPV6, ipv6_ips))
    else:
        logger.info("IPv6 not available on all nodes - skipping IPv6 iteration")

    return configs


def _get_random_dscp_not_default() -> Tuple[Any, int]:
    """Get a random DSCP value (enum or numeric) excluding default (46)."""
    default_value = DscpMarkingConstants.DEFAULT_VALUE
    use_enum = random.choice([True, False])

    if use_enum:
        valid_enums = [
            e for e in DscpMarkingConstants.DSCP_ENUM_NAMES
            if DscpMarkingConstants.DSCP_ENUM_MAP[e][0] != default_value
        ]
        enum_name = random.choice(valid_enums)
        numeric = DscpMarkingConstants.DSCP_ENUM_MAP[enum_name][0]
        return enum_name, numeric
    else:
        valid_range = [
            v for v in range(DscpMarkingConstants.MIN_VALUE,
                             DscpMarkingConstants.MAX_VALUE + 1)
            if v != default_value
        ]
        numeric = random.choice(valid_range)
        return numeric, numeric


def _start_captures_on_all_nodes(ctx: DscpVerificationContext) -> None:
    """Start nv-bridge packet capture on all nodes."""
    for idx, engine in enumerate(ctx.engines_list):
        capture_file = ctx.ip_version.get_capture_file(idx)
        ctx.capture_files[engine.ip] = capture_file
        ControlPlaneTrafficTools.start_nv_bridge_capture(
            engine, ctx.interface, capture_file,
            duration_seconds=ctx.capture_duration_seconds,
            ipv6=ctx.ip_version.is_ipv6
        )


def _trigger_traffic_on_all_nodes(ctx: DscpVerificationContext) -> None:
    """Trigger cluster operations on all nodes to generate traffic."""
    for engine in ctx.engines_list:
        ControlPlaneTrafficTools.trigger_cluster_operations(engine, ctx.cluster)
    time.sleep(ctx.capture_duration_seconds)


def _verify_captures_on_all_nodes(ctx: DscpVerificationContext) -> Dict[str, TrafficVerificationResult]:
    """Stop captures and verify DSCP marking on all nodes."""
    results = {}
    for engine in ctx.engines_list:
        capture_file = ctx.capture_files.get(engine.ip)
        PacketCaptureTool.stop_capture(engine, capture_file=capture_file)
        time.sleep(DscpMarkingConstants.CAPTURE_FINALIZE_DELAY_SECONDS)

        capture_file = ctx.capture_files[engine.ip]
        found, pkt_count, snippet = DscpCaptureVerifier.verify_dscp_in_capture(
            engine, capture_file, ctx.dscp_numeric, ipv6=ctx.ip_version.is_ipv6
        )
        results[engine.ip] = TrafficVerificationResult(
            ip=engine.ip, found=found, packets=pkt_count, snippet=snippet
        )
        PacketCaptureTool.cleanup_capture_file(engine, capture_file)
    return results


def _validate_dscp_results(results: Dict[str, TrafficVerificationResult],
                           dscp_numeric: int, ip_version: IpVersion) -> None:
    """Validate that DSCP marking was found on all nodes."""
    expected_tos = dscp_numeric << 2
    total_packets = sum(r.packets for r in results.values())
    all_dscp_found = all(r.found for r in results.values())
    nodes_without_dscp = [ip for ip, r in results.items() if not r.found]

    results_summary = {ip: f"packets={r.packets}, found={r.found}" for ip, r in results.items()}

    logger.info(f"{ip_version.label} DSCP Verification: total_packets={total_packets}, "
                f"dscp={dscp_numeric}, all_found={all_dscp_found}")

    assert total_packets > 0, (
        f"No {ip_version.label} packets captured. Results: {results_summary}"
    )
    assert all_dscp_found, (
        f"{ip_version.label} DSCP {dscp_numeric} (TOS 0x{expected_tos:02x}) not found on all nodes. "
        f"Missing: {nodes_without_dscp}. Results: {results_summary}"
    )


def _run_dscp_verification(engines_list: List[Any], interface: str,
                           dscp_numeric: int, ip_version: IpVersion,
                           cluster: Cluster) -> Dict[str, TrafficVerificationResult]:
    """Run DSCP verification: capture nv-bridge traffic, trigger operations, verify marking."""
    capture_duration = DscpMarkingConstants.compute_capture_duration_seconds(len(engines_list))
    ctx = DscpVerificationContext(
        engines_list=engines_list,
        interface=interface,
        dscp_numeric=dscp_numeric,
        ip_version=ip_version,
        cluster=cluster,
        capture_duration_seconds=capture_duration,
    )

    with allure.step(f"{ip_version.label}: Start capture on all nodes"):
        _start_captures_on_all_nodes(ctx)

    with allure.step(f"{ip_version.label}: Generate traffic"):
        _trigger_traffic_on_all_nodes(ctx)

    with allure.step(f"{ip_version.label}: Verify DSCP in captures"):
        results = _verify_captures_on_all_nodes(ctx)

    with allure.step(f"{ip_version.label}: Validate results"):
        _validate_dscp_results(results, dscp_numeric, ip_version)

    return results


def _configure_cluster_for_iteration(
    nmxc_engine: Any,
    secondary_engines: List[Any],
    ip_config: ClusterIpVersionConfig,
    dscp_test_value: Any,
    cluster: Cluster
) -> None:
    """Configure cluster with IP version-specific addresses and DSCP value."""
    label = ip_config.label
    node_ips = ip_config.node_ips

    with allure.step(f"{label}: Configure cluster on main node ({nmxc_engine.ip})"):
        with allure.step(f"Set cluster state enabled on {nmxc_engine.ip}"):
            ClusterConfigHelper.set_cluster_state(
                cluster, "enabled", apply=False, dut_engine=nmxc_engine
            ).verify_result()

        with allure.step(f"Set DSCP {dscp_test_value} on {nmxc_engine.ip}"):
            success = ControlPlaneTrafficTools.configure_dscp_on_node(
                nmxc_engine, dscp_test_value, cluster=cluster, apply=False
            )
            assert success, f"Failed to configure DSCP {dscp_test_value} on {nmxc_engine.ip}"

        with allure.step(f"Add {len(node_ips)} cluster node(s) with {label} addresses"):
            for ip in node_ips:
                ClusterConfigHelper.add_cluster_node_server(
                    ip, cluster=cluster, apply=False, dut_engine=nmxc_engine
                )

        with allure.step(f"Apply configuration on main node {nmxc_engine.ip}"):
            ClusterConfigHelper.apply_config(nmxc_engine)
            time.sleep(DscpMarkingConstants.CLUSTER_OPERATION_DELAY_SECONDS)
            ClusterConfigHelper.save_config(nmxc_engine)

    if secondary_engines:
        OpenApiRequest.clear_changeset_and_payload()

        with allure.step(f"{label}: Configure DSCP on {len(secondary_engines)} secondary node(s)"):
            for engine in secondary_engines:
                OpenApiRequest.clear_changeset_and_payload()

                with allure.step(f"Set DSCP {dscp_test_value} on {engine.ip}"):
                    success = ControlPlaneTrafficTools.configure_dscp_on_node(
                        engine, dscp_test_value, cluster=cluster, apply=False
                    )
                    assert success, f"Failed to configure DSCP {dscp_test_value} on {engine.ip}"

                with allure.step(f"Apply configuration on {engine.ip}"):
                    ClusterConfigHelper.apply_config(engine)
                    ClusterConfigHelper.save_config(engine)


def _run_iteration(
    engines_list: List[Any],
    nmxc_engine: Any,
    secondary_engines: List[Any],
    ip_config: ClusterIpVersionConfig,
    dscp_test_value: Any,
    dscp_numeric: int,
    interface: str,
    cluster: Cluster,
    facade: DscpMarkingTestFacade,
) -> None:
    """Run a single DSCP verification iteration: configure, verify, traffic test, cleanup."""
    label = ip_config.label
    ip_version = ip_config.ip_version

    logger.info(f"=== Starting {label} Iteration (nodes: {ip_config.node_ips}) ===")

    try:
        _configure_cluster_for_iteration(
            nmxc_engine, secondary_engines, ip_config, dscp_test_value, cluster
        )

        with allure.step(f"{label}: Verify DSCP value after configuration"):
            facade.verify_dscp_value(dscp_numeric)

        with allure.step(f"{label}: Wait for cluster connectivity"):
            conn_established = ClusterConfigHelper.wait_for_cluster_connection(
                nmxc_engine, cluster
            )
            assert conn_established, (
                f"[{label}] Cluster connection (nmxc-conn) not established on {nmxc_engine.ip} "
                f"after {DscpMarkingConstants.CLUSTER_CONN_MAX_RETRIES} retries"
            )

        with allure.step(f"{label}: Verify tc configuration on {interface}"):
            if facade.interface_exists(interface):
                has_prio = facade.verify_qdisc_prio(interface)
                assert has_prio, f"Expected prio qdisc on {interface}"
                tc_filter = facade.get_tc_filter(interface)
                logger.info(f"[{label}] tc filter on {interface}:\n{tc_filter}")

        with allure.step(f"{label}: nv-bridge traffic verification"):
            _run_dscp_verification(
                engines_list, interface, dscp_numeric, ip_version, cluster
            )

    finally:
        with allure.step(f"{label}: Cleanup cluster configuration"):
            ControlPlaneTrafficTools.cleanup_cluster_config_on_all_nodes(engines_list, cluster=cluster)
            time.sleep(DscpMarkingConstants.CLUSTER_OPERATION_DELAY_SECONDS)

    logger.info(f"=== {label} Iteration Completed ===")


@pytest.mark.nmx
@pytest.mark.cluster_dscp
@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.air
def test_dscp_marking_complete_flow(engines, devices, dut_engines, random_api):
    """
    Comprehensive DSCP marking validation for IPv4 and IPv6.

    Verifies default state, configures a random DSCP value on all cluster nodes,
    captures nv-bridge traffic (port 50052), and asserts TOS field matches.
    Runs for both IPv4 and IPv6 (if available). Skips traffic verification on single-switch.
    """
    TestToolkit.tested_api = random_api

    dscp_test_value, dscp_numeric = _get_random_dscp_not_default()

    switch_pairs = PacketCaptureTool.get_switch_pairs(dut_engines)
    has_multiple_switches = len(switch_pairs) > 0
    engines_list = list(dut_engines.values())
    nmxc_engine = engines_list[0]
    secondary_engines = engines_list[1:] if len(engines_list) > 1 else []

    cluster = Cluster()
    facade = DscpMarkingTestFacade(cluster, engines, devices)
    mgmt_ports = facade.get_mgmt_ports() or [DscpMarkingConstants.DEFAULT_MGMT_INTERFACE]
    interface = mgmt_ports[0]

    logger.info(f"Test config: DSCP={dscp_test_value}({dscp_numeric}), "
                f"switches={len(engines_list)}, interface={interface}, "
                f"multi_switch={has_multiple_switches}")

    with allure.step("Verify initial state - default DSCP value"):
        facade.verify_dscp_value(DscpMarkingConstants.DEFAULT_VALUE)

    if not has_multiple_switches:
        logger.info("Single switch setup - traffic verification skipped")
        return

    ip_configs = _get_cluster_ip_configs(engines_list, interface)
    logger.info(f"Running {len(ip_configs)} iteration(s): {[c.label for c in ip_configs]}")

    try:
        for iteration_num, ip_config in enumerate(ip_configs, start=1):
            with allure.step(f"Iteration {iteration_num}: {ip_config.label} DSCP Verification"):
                _run_iteration(
                    engines_list=engines_list,
                    nmxc_engine=nmxc_engine,
                    secondary_engines=secondary_engines,
                    ip_config=ip_config,
                    dscp_test_value=dscp_test_value,
                    dscp_numeric=dscp_numeric,
                    interface=interface,
                    cluster=cluster,
                    facade=facade,
                )
    finally:
        with allure.step("Final cleanup"):
            ControlPlaneTrafficTools.cleanup_cluster_config_on_all_nodes(engines_list, cluster=cluster)


@pytest.mark.nmx
@pytest.mark.cluster_dscp
@pytest.mark.timeout(5 * MINUTE, func_only=True)
@pytest.mark.parametrize("invalid_value,description", INVALID_DSCP_TEST_VALUES)
@pytest.mark.air
def test_dscp_marking_invalid_value(engines, devices, random_api, invalid_value, description):
    """Validate that an invalid DSCP value is rejected and config remains unchanged."""
    TestToolkit.tested_api = random_api

    with allure.step("Create test components"):
        cluster = Cluster()
        facade = DscpMarkingTestFacade(cluster, engines, devices)

    try:
        with allure.step("Record initial DSCP value"):
            initial_value = facade.get_dscp_value()

        with allure.step(f"Test invalid value: {description} ({invalid_value})"):
            verify_invalid_dscp_rejected(engines, invalid_value, description)

        with allure.step("Verify configuration unchanged"):
            final_value = facade.get_dscp_value()
            initial_numeric = DscpValueSelector.get_numeric_value(initial_value)
            final_numeric = DscpValueSelector.get_numeric_value(final_value)
            assert final_numeric == initial_numeric, \
                f"Config corrupted by invalid value! Initial: {initial_numeric}, Final: {final_numeric}"

    finally:
        with allure.step("Cleanup"):
            facade.cleanup()
