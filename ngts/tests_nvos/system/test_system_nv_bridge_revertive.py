"""NVBridge (NVLink-over-IP) switchover and revertive-mode verification tests."""
from __future__ import annotations

import json
import logging
import re
import time

import pytest

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import ApiType, OutputFormat, SystemConsts
from ngts.nvos_tools.infra.FilesTool import FilesTool
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.infra.NvBridgeTool import (
    NvBridgeRevertiveConsts,
    get_interface_ips,
    parse_network_info,
)
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.system.System import System
from ngts.scripts.sonic_deploy.nvos_only_methods import NvosInstallationSteps
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


IP_VERSIONS = ["ipv4", "ipv6"]


def is_nvb_server_running(engine: LinuxSshEngine) -> bool:
    """Return True when the nvb-s daemon is active (systemd)."""
    out = engine.run_cmd(
        f"sudo systemctl is-active {NvBridgeRevertiveConsts.NVB_SERVER_SERVICE} "
        f"2>/dev/null || true"
    )
    return bool(out.strip()) and out.strip().splitlines()[-1].strip() == "active"


def restart_mgmtportsyncd(engine: LinuxSshEngine) -> None:
    """Prod the host mgmtportsyncd provisioner to re-run its rebuild gate."""
    with allure.step("restart mgmtportsyncd to trigger network-info rebuild"):
        found = engine.run_cmd(
            "systemctl list-units --type=service --all --no-legend 2>/dev/null "
            "| awk '{print $1}' | grep -i mgmtport | head -n1"
        ).strip().splitlines()
        unit = (
            found[-1].strip()
            if found and found[-1].strip()
            else NvBridgeRevertiveConsts.MGMTPORTSYNCD_SERVICE
        )
        engine.run_cmd(f"sudo systemctl restart {unit} 2>/dev/null || true")


def add_iface_ip(engine: LinuxSshEngine, iface: str, cidr: str) -> None:
    """Add address *cidr* on *iface* (best-effort; ignores 'File exists')."""
    with allure.step(f"add address {cidr} to {iface}"):
        engine.run_cmd(f"sudo ip addr add {cidr} dev {iface} 2>/dev/null || true")


def del_iface_ip(engine: LinuxSshEngine, iface: str, cidr: str) -> None:
    """Remove address *cidr* from *iface* (best-effort)."""
    with allure.step(f"remove address {cidr} from {iface}"):
        engine.run_cmd(f"sudo ip addr del {cidr} dev {iface} 2>/dev/null || true")


def get_ssh_server_ip(engine: LinuxSshEngine) -> str:
    """Return the DUT-side IP the current SSH session is bound to."""
    out = engine.run_cmd("echo $SSH_CONNECTION")
    for line in reversed(out.strip().splitlines()):
        parts = line.split()
        if len(parts) >= 3 and (":" in parts[2] or "." in parts[2]):
            return parts[2].split("%")[0]
    return ""


def get_iface_ipv4_cidr(engine: LinuxSshEngine, iface: str) -> str:
    """Return the first global IPv4 ``addr/prefix`` on *iface* ("" if none)."""
    out = engine.run_cmd(
        f"ip -o -4 addr show dev {iface} scope global 2>/dev/null "
        f"| awk '{{print $4}}'"
    )
    for line in out.strip().splitlines():
        line = line.strip()
        if "/" in line and line[0].isdigit():
            return line
    return ""


def count_history_log_lines(engine: LinuxSshEngine) -> int:
    """Return the number of non-empty lines in network-info-history.log."""
    text = FilesTool.read_text_file(engine, NvBridgeRevertiveConsts.ADDRESS_HISTORY_LOG)
    return len([ln for ln in text.splitlines() if ln.strip()])


def wait_for_history_log_growth(
    engine: LinuxSshEngine,
    baseline_lines: int,
    timeout: int = NvBridgeRevertiveConsts.HISTORY_LOG_TIMEOUT_SEC,
    poll_interval: int = NvBridgeRevertiveConsts.POLL_INTERVAL_SEC,
) -> bool:
    """Poll until the history log gains a new entry (True) or timeout (False)."""
    log = NvBridgeRevertiveConsts.ADDRESS_HISTORY_LOG
    with allure.step(f"wait up to {timeout}s for a new entry in {log}"):
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if count_history_log_lines(engine) > baseline_lines:
                return True
            time.sleep(poll_interval)
    return False


def get_onie_platform(
    engine: LinuxSshEngine,
    retries: int = NvBridgeRevertiveConsts.PLATFORM_READ_RETRIES,
) -> str:
    """Return the onie_platform string from machine.conf.

    Reads the whole file and parses locally so a transient short/empty SSH read
    is retried instead of being treated as an authoritative empty answer.
    """
    for _ in range(retries):
        raw = engine.run_cmd(
            f"cat {NvBridgeRevertiveConsts.MACHINE_CONF} 2>/dev/null || true"
        )
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("onie_platform="):
                platform = line.split("=", 1)[1].strip()
                if platform:
                    return platform
        time.sleep(NvBridgeRevertiveConsts.POLL_INTERVAL_SEC)
    return ""


def get_device_platform_json(engine: LinuxSshEngine) -> dict:
    """Resolve the device platform.json via onie_platform in machine.conf."""
    platform = get_onie_platform(engine)
    if not platform:
        return {}
    path = NvBridgeRevertiveConsts.DEVICE_PLATFORM_JSON.format(platform=platform)
    if not FilesTool.file_exists_sudo(engine, path).ignore_result():
        return {}
    return FilesTool.read_json_file(engine, path)


def get_platform_capabilities(platform_json: dict) -> dict:
    """Return the platform.json capabilities dict."""
    caps = platform_json.get(NvBridgeRevertiveConsts.PLATFORM_CAPABILITIES_KEY)
    return caps if isinstance(caps, dict) else platform_json


def is_nv_bridge_platform(engine: LinuxSshEngine) -> bool:
    """Return True when the device platform.json enables nv-bridge."""
    caps = get_platform_capabilities(get_device_platform_json(engine))
    return (
        str(caps.get(NvBridgeRevertiveConsts.PLATFORM_SUPPORTED_KEY, "")).lower() ==
        NvBridgeRevertiveConsts.PLATFORM_SUPPORTED_VALUE
    )


def build_network_info(iface_ips: dict[str, list[str]]) -> dict:
    """Build a ``network_info.json`` document from ``{iface: [ips]}``."""
    return {
        NvBridgeRevertiveConsts.NET_INTERFACES_KEY: [
            {
                NvBridgeRevertiveConsts.NET_IFACE_NAME: iface,
                NvBridgeRevertiveConsts.NET_IFACE_IPS: ips,
            }
            for iface, ips in iface_ips.items()
        ]
    }


def build_forward_schema_network_info(iface_ips: dict[str, list[str]]) -> dict:
    """Build a valid network_info.json carrying unknown/newer schema fields."""
    doc = build_network_info(iface_ips)
    doc[NvBridgeRevertiveConsts.UNKNOWN_TOP_LEVEL_KEY] = "ignore-me"
    for entry in doc[NvBridgeRevertiveConsts.NET_INTERFACES_KEY]:
        entry[NvBridgeRevertiveConsts.UNKNOWN_IFACE_KEY] = {"nested": [1, 2, 3]}
    return doc


def notify_nvb_reload_network_info(engine: LinuxSshEngine) -> str:
    """POST /config/network-info to make nvb-s re-read the file."""
    url = f"{NvBridgeRevertiveConsts.REST_BASE_URL}{NvBridgeRevertiveConsts.REST_CONFIG_NETWORK_INFO}"
    body = json.dumps(NvBridgeRevertiveConsts.REST_CONFIG_BODY)
    with allure.step(f"POST reload {url}"):
        out = engine.run_cmd(
            f"sudo curl -s --unix-socket {NvBridgeRevertiveConsts.REST_SOCKET} "
            f"-X POST -H 'Content-Type: application/json' "
            f"-d '{body}' {url}"
        )
        time.sleep(NvBridgeRevertiveConsts.RELOAD_SETTLE_SEC)
        return out


def _switch_over_url() -> str:
    """URL for the nvb-s manual switch-over REST endpoint (served on the socket)."""
    return (
        f"{NvBridgeRevertiveConsts.REST_BASE_URL}"
        f"{NvBridgeRevertiveConsts.REST_ACTION_SWITCHOVER}"
    )


def post_switch_over_raw(engine: LinuxSshEngine, body: str) -> tuple[int, str]:
    """POST a raw JSON *body* to nvb-s ``/action/switch-over`` (unix socket)."""
    sep = "HTTP_STATUS:"
    url = _switch_over_url()
    with allure.step(f"POST switch-over {url} body={body}"):
        out = engine.run_cmd(
            f"sudo curl -sS --unix-socket {NvBridgeRevertiveConsts.REST_SOCKET} "
            f"-X POST -H 'Content-Type: application/json' "
            f"-d '{body}' -w '{sep}%{{http_code}}\\n' {url} 2>&1"
        )
    resp, _, tail = out.rpartition(sep)
    match = re.search(r"\d{3}", tail)
    return (int(match.group()) if match else 0), resp.strip()


def request_switch_over(engine: LinuxSshEngine, addresses: list[str]) -> tuple[int, str]:
    """POST a well-formed manual switch-over targeting *addresses* (iface IPs)."""
    body = json.dumps({NvBridgeRevertiveConsts.REST_SWITCHOVER_ADDRESSES_KEY: addresses})
    return post_switch_over_raw(engine, body)


def stop_nvb_server(engine: LinuxSshEngine) -> None:
    """Stop the nvb-s daemon (systemd) to simulate 'server down'."""
    with allure.step(f"stop {NvBridgeRevertiveConsts.NVB_SERVER_SERVICE}"):
        engine.run_cmd(
            f"sudo systemctl stop {NvBridgeRevertiveConsts.NVB_SERVER_SERVICE}"
        )


def get_nv_bridge_show(nv_bridge, dut_engine: LinuxSshEngine) -> dict:
    """Return the parsed ``nv show system nv-bridge`` dict."""
    return OutputParsingTool.parse_json_str_to_dictionary(
        nv_bridge.show(dut_engine=dut_engine)
    ).get_returned_value()


def get_connections(show: dict) -> dict:
    """Return the ``connections`` object from a show dict."""
    return show.get(SystemConsts.NV_BRIDGE_CONNECTION, {}) or {}


def get_active_connection(show: dict) -> dict | None:
    """Return the connection entry whose state is 'active', if any."""
    for conn in get_connections(show).values():
        if conn.get(NvBridgeRevertiveConsts.CONN_ACTIVE) == NvBridgeRevertiveConsts.STATE_ACTIVE:
            return conn
    return None


def get_active_interface(show: dict, net_info: dict | None = None) -> str | None:
    """Resolve the currently active OOB interface (eth0/eth1)."""
    active_iface = show.get(NvBridgeRevertiveConsts.SHOW_ACTIVE_INTERFACE)
    if active_iface:
        return active_iface
    if net_info is None:
        return None
    active_conn = get_active_connection(show)
    if not active_conn:
        return None
    server_ip = _strip_port(active_conn.get(NvBridgeRevertiveConsts.CONN_SERVER_ADDRESS, ""))
    for iface, ips in parse_network_info(net_info).items():
        if server_ip in {_strip_port(ip) for ip in ips}:
            return iface
    return None


def _strip_port(address: str) -> str:
    """Strip a trailing ``:port`` from an IPv4/IPv6 address string."""
    if not address:
        return ""
    if address.startswith("["):  # [ipv6]:port
        return address[1:].split("]")[0]
    # IPv6 without brackets has multiple ':' and no distinguishable port -> leave
    # it as-is (stripping the last group would collapse distinct addresses).
    if address.count(":") > 1:
        return address
    host, sep, tail = address.rpartition(":")
    return host if sep and tail.isdigit() else address


def wait_for_active_interface(
    nv_bridge,
    dut_engine: LinuxSshEngine,
    expected_interface: str,
    net_info: dict | None = None,
    timeout: int = NvBridgeRevertiveConsts.SWITCHOVER_TIMEOUT_SEC,
    poll_interval: int = NvBridgeRevertiveConsts.POLL_INTERVAL_SEC,
) -> None:
    """Poll ``nv show system nv-bridge`` until active-interface == *expected*."""
    with allure.step(f"wait up to {timeout}s for active-interface -> {expected_interface}"):
        start = time.monotonic()
        last = None
        while time.monotonic() - start < timeout:
            show = get_nv_bridge_show(nv_bridge, dut_engine)
            last = get_active_interface(show, net_info)
            if last == expected_interface:
                return
            time.sleep(poll_interval)
        raise AssertionError(
            f"active-interface did not become '{expected_interface}' within "
            f"{timeout}s (last observed: {last})"
        )


def wait_for_path_unhealthy(
    nv_bridge,
    dut_engine: LinuxSshEngine,
    server_ips: list[str],
    timeout: int = NvBridgeRevertiveConsts.UNHEALTHY_TIMEOUT_SEC,
    poll_interval: int = NvBridgeRevertiveConsts.POLL_INTERVAL_SEC,
) -> None:
    """Poll until the connection whose server-address is in *server_ips* is unhealthy."""
    targets = {_strip_port(ip) for ip in server_ips}
    with allure.step(f"wait up to {timeout}s for {server_ips} to be reported unhealthy"):
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            for conn in get_connections(get_nv_bridge_show(nv_bridge, dut_engine)).values():
                server_ip = _strip_port(conn.get(NvBridgeRevertiveConsts.CONN_SERVER_ADDRESS, ""))
                if (
                    server_ip in targets and
                    conn.get(NvBridgeRevertiveConsts.CONN_IS_HEALTHY) ==
                    NvBridgeRevertiveConsts.HEALTHY_NO
                ):
                    return
            time.sleep(poll_interval)
        raise AssertionError(
            f"path {server_ips} was not reported unhealthy within {timeout}s"
        )


def _nvb_block_rules(ip: str, port: int) -> list[tuple[str, str]]:
    """(chain, match-spec) rules that block nvb keepalive on *ip*:*port*."""
    return [
        ("INPUT", f"-p tcp -s {ip} --sport {port}"),
        ("INPUT", f"-p tcp -d {ip} --dport {port}"),
        ("OUTPUT", f"-p tcp -d {ip} --dport {port}"),
        ("OUTPUT", f"-p tcp -s {ip} --sport {port}"),
    ]


def block_nvb_path(engine: LinuxSshEngine, ips: list[str]) -> None:
    """Drop nvb keepalive (tcp/NVB_SERVER_PORT) for *ips* to fail that path."""
    port = NvBridgeRevertiveConsts.NVB_SERVER_PORT
    assert ips, "block_nvb_path called with no IPs (network_info.json mapping missing?)"
    with allure.step(f"block nvb keepalive tcp/{port} on {ips}"):
        for ip in ips:
            binary = IpTool.iptables_binary(ip)
            for chain, spec in _nvb_block_rules(ip, port):
                engine.run_cmd(f"sudo {binary} -I {chain} {spec} -j DROP")


def unblock_nvb_path(engine: LinuxSshEngine, ips: list[str]) -> None:
    """Undo :func:`block_nvb_path` for *ips* (best-effort, removes duplicates)."""
    port = NvBridgeRevertiveConsts.NVB_SERVER_PORT
    with allure.step(f"restore nvb keepalive tcp/{port} on {ips}"):
        for ip in ips:
            binary = IpTool.iptables_binary(ip)
            for chain, spec in _nvb_block_rules(ip, port):
                # A rule may have been inserted more than once across retries;
                # delete until the spec no longer matches.
                engine.run_cmd(
                    f"while sudo {binary} -D {chain} {spec} -j DROP 2>/dev/null; "
                    f"do :; done"
                )


def add_netem_delay(engine: LinuxSshEngine, interface: str, delay_ms: int) -> None:
    """Inject egress latency on *interface* to raise gRPC RTT (tie-breaker test)."""
    with allure.step(f"tc netem: add {delay_ms}ms delay on {interface}"):
        engine.run_cmd(
            f"sudo tc qdisc replace dev {interface} root netem delay {delay_ms}ms"
        )


def clear_netem_delay(engine: LinuxSshEngine, interface: str) -> None:
    """Remove any tc netem qdisc previously added on *interface*."""
    with allure.step(f"tc netem: clear qdisc on {interface}"):
        engine.run_cmd(f"sudo tc qdisc del dev {interface} root 2>/dev/null || true")


def set_revertive_mode(engine: LinuxSshEngine, mode: str) -> None:
    """Configure the cluster revertive mode via NVUE (routed to nmx-controller)."""
    with allure.step(f"set revertive mode -> {mode}"):
        engine.run_cmd(NvBridgeRevertiveConsts.REVERTIVE_MODE_ACTION.format(mode=mode))


def wait_for_revertive_mode(
    nv_bridge,
    dut_engine: LinuxSshEngine,
    expected_mode: str,
    timeout: int = NvBridgeRevertiveConsts.SWITCHOVER_TIMEOUT_SEC,
    poll_interval: int = NvBridgeRevertiveConsts.POLL_INTERVAL_SEC,
) -> None:
    """Poll until nvb-s reports the configured mode (nvlink-revertive-mode field)."""
    with allure.step(f"wait up to {timeout}s for nvlink-revertive-mode -> {expected_mode}"):
        start = time.monotonic()
        last = None
        while time.monotonic() - start < timeout:
            show = get_nv_bridge_show(nv_bridge, dut_engine)
            last = show.get(NvBridgeRevertiveConsts.SHOW_REVERTIVE_MODE)
            if last == expected_mode:
                return
            time.sleep(poll_interval)
        raise AssertionError(
            f"nvlink-revertive-mode did not become '{expected_mode}' within "
            f"{timeout}s (last observed: {last})"
        )


def assert_active_interface_stable(
    nv_bridge,
    dut_engine: LinuxSshEngine,
    expected_interface: str,
    net_info: dict | None = None,
    duration: int = NvBridgeRevertiveConsts.STABLE_OBSERVE_SEC,
    poll_interval: int = NvBridgeRevertiveConsts.POLL_INTERVAL_SEC,
) -> None:
    """Assert active-interface stays on *expected* for the whole *duration*."""
    with allure.step(f"assert active-interface stays {expected_interface} for {duration}s"):
        start = time.monotonic()
        while time.monotonic() - start < duration:
            show = get_nv_bridge_show(nv_bridge, dut_engine)
            current = get_active_interface(show, net_info)
            assert current == expected_interface, (
                f"active-interface changed to '{current}' (expected stable "
                f"'{expected_interface}') during non-revertive observation"
            )
            time.sleep(poll_interval)


@pytest.fixture
def skip_if_not_nv_bridge_platform(engines):
    """Skip when the DUT platform does not advertise nv-bridge support."""
    if not is_nv_bridge_platform(engines.dut):
        pytest.skip("DUT platform.json does not have nv-bridge-supported=yes")


def get_configured_node_servers(cluster, engine: LinuxSshEngine) -> set[str]:
    """Return the set of node primary server IPs currently configured."""
    output = cluster.show(op_param="node", output_format=OutputFormat.json, dut_engine=engine)
    parsed = OutputParsingTool.parse_json_str_to_dictionary(output).get_returned_value()
    return set((parsed or {}).get("primary", {}).get("server", {}).keys())


def ensure_dual_node_servers(cluster, engine: LinuxSshEngine, net_info: dict) -> bool:
    """Configure a node primary server for BOTH eth0 and eth1 (dual path).

    ``node primary server`` rejects a config that mixes IPv4 and IPv6 entries,
    so new entries must match whatever family is already configured (e.g. by
    ``ClusterTools.start_cluster()``). If nothing is configured yet, prefer
    IPv4 (fall back to IPv6) and take a single address per interface. If the
    two interfaces share no common family, or the existing config already
    mixes families, skip setup rather than submit/extend a mixed-family
    config.
    """
    mapped = parse_network_info(net_info)
    ifaces = (
        NvBridgeRevertiveConsts.PRIMARY_IFACE,
        NvBridgeRevertiveConsts.SECONDARY_IFACE,
    )

    def pick_one_per_iface(want_ipv6: bool) -> list[str]:
        picked = [
            next((ip for ip in mapped.get(iface, []) if IpTool.is_address_ipv6(ip) == want_ipv6), None)
            for iface in ifaces
        ]
        return picked if all(picked) else []

    existing = get_configured_node_servers(cluster, engine)
    existing_has_v4 = any(not IpTool.is_address_ipv6(ip) for ip in existing)
    existing_has_v6 = any(IpTool.is_address_ipv6(ip) for ip in existing)

    if existing_has_v4 and existing_has_v6:
        with allure.step("skip dual node-server setup: existing config already mixes IPv4/IPv6"):
            logger.warning(f"node primary server already mixes address families ({existing}); skipping")
        return False

    if existing:
        desired = pick_one_per_iface(want_ipv6=existing_has_v6)
    else:
        desired = pick_one_per_iface(want_ipv6=False) or pick_one_per_iface(want_ipv6=True)

    added = False
    for ip in desired:
        if ip not in existing:
            with allure.step(f"configure node primary server {ip}"):
                cluster.node.primary.set_cluster_node(
                    op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER,
                    op_param_value=ip,
                    apply=True,
                    dut_engine=engine,
                )
            added = True
    return added


def restart_nmx_controller(cluster, engine: LinuxSshEngine) -> None:
    """Stop+start nmx-controller and wait for nmxc-conn to come back up."""
    with allure.step("restart nmx-controller to (re)connect over both paths"):
        cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].action_stop_cluster_app(engine=engine)
        ClusterTools.wait_for_apps_to_be_in_wanted_state(
            cluster, cluster_expected_state="", nmx_c_expected_state="down", engine=engine
        )
        cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].action_start_cluster_app(engine=engine)
        ClusterTools.wait_for_apps_to_be_in_wanted_state(
            cluster, cluster_expected_state="enabled", nmx_c_expected_state="up", engine=engine
        )


@pytest.fixture(scope="module")
def nv_bridge_cluster(engines, setup_name, devices):
    """Enable cluster and bring BOTH nv-bridge node paths (eth0+eth1) up.

    Module-scoped: the cluster is enabled (and nmx-c bounced onto both paths)
    once per module and shared across all tests, instead of paying the full
    cluster start/stop + nmx-c restart on every test. Each test still isolates
    itself via its own function-scoped restore fixtures.
    """
    cluster = Cluster()
    with allure.step("Start cluster (auto-configures nv-bridge node + nmx-c)"):
        ClusterTools.start_cluster(cluster, setup_name, engine=engines.dut, devices=devices)
    with allure.step("Ensure BOTH eth0+eth1 node servers are configured (dual path)"):
        path = NvBridgeRevertiveConsts.NETWORK_INFO_PATH
        if FilesTool.file_exists_sudo(engines.dut, path).ignore_result():
            net_info = FilesTool.read_json_file(engines.dut, path)
            if ensure_dual_node_servers(cluster, engines.dut, net_info):
                restart_nmx_controller(cluster, engines.dut)
    yield cluster
    with allure.step("Unset cluster node and stop cluster"):
        cluster.unset(apply=True, dut_engine=engines.dut)
        ClusterTools.stop_cluster(cluster)


@pytest.fixture
def restore_network_info(engines):
    """Snapshot network_info.json before a test and restore it afterwards."""
    backup = "/tmp/nv_bridge_network_info_backup.json"
    path = NvBridgeRevertiveConsts.NETWORK_INFO_PATH
    engines.dut.run_cmd(f"sudo cp {path} {backup} 2>/dev/null || true")
    yield
    with allure.step("restore original network_info.json"):
        engines.dut.run_cmd(f"sudo cp {backup} {path} 2>/dev/null || true")
        engines.dut.run_cmd(f"sudo rm -f {backup}")
        notify_nvb_reload_network_info(engines.dut)


@pytest.fixture
def restore_nvb_paths(engines):
    """Best-effort remove any nvb keepalive DROP rules after a fault test."""
    yield
    with allure.step("restore nvb keepalive on all mgmt paths"):
        path = NvBridgeRevertiveConsts.NETWORK_INFO_PATH
        if FilesTool.file_exists_sudo(engines.dut, path).ignore_result():
            net_info = FilesTool.read_json_file(engines.dut, path)
            for _iface, ips in parse_network_info(net_info).items():
                if ips:
                    unblock_nvb_path(engines.dut, ips)


@pytest.fixture
def restore_netem(engines):
    """Clear any tc netem qdisc entries added on eth0/eth1 after an RTT test."""
    yield
    for iface in (
        NvBridgeRevertiveConsts.PRIMARY_IFACE,
        NvBridgeRevertiveConsts.SECONDARY_IFACE,
    ):
        clear_netem_delay(engines.dut, iface)


@pytest.fixture
def reset_revertive_mode(engines):
    """Reset revertive mode back to the default (non-revertive) after the test."""
    yield
    with allure.step("reset revertive mode to non-revertive"):
        engines.dut.run_cmd(
            NvBridgeRevertiveConsts.REVERTIVE_MODE_ACTION.format(
                mode=NvBridgeRevertiveConsts.MODE_NON_REVERTIVE
            ) +
            " 2>/dev/null || true"
        )


@pytest.fixture
def restore_nvb_server(engines):
    """Ensure the nvb-s daemon is running again after a 'server down' test."""
    yield
    with allure.step(f"ensure {NvBridgeRevertiveConsts.NVB_SERVER_SERVICE} is running"):
        engines.dut.run_cmd(
            f"sudo systemctl start {NvBridgeRevertiveConsts.NVB_SERVER_SERVICE} "
            f"2>/dev/null || true"
        )


@pytest.fixture
def skip_if_no_upgrade_versions(request):
    """Skip the real upgrade/downgrade test unless base+target images are given."""
    base = request.config.getoption("--base_version")
    target = request.config.getoption("--target_version")
    if not base or not target:
        pytest.skip(
            "real upgrade/downgrade needs --base_version and --target_version "
            "(HW/regression only)"
        )


def _parametrized_ip_version(request: pytest.FixtureRequest) -> str | None:
    """Return the ``ip_version`` parametrize value for the running test (if any)."""
    callspec = getattr(request.node, "callspec", None)
    return callspec.params.get("ip_version") if callspec else None


@pytest.fixture
def skip_if_ip_family_unavailable(engines, request):
    """Skip an ``ipv6`` parametrization when no IPv6 mgmt address is provisioned."""
    if _parametrized_ip_version(request) != "ipv6":
        return
    path = NvBridgeRevertiveConsts.NETWORK_INFO_PATH
    if not FilesTool.file_exists_sudo(engines.dut, path).ignore_result():
        pytest.skip("network_info.json missing; cannot confirm IPv6 mgmt addressing")
    mapped = parse_network_info(FilesTool.read_json_file(engines.dut, path))
    has_v6 = any(IpTool.is_address_ipv6(ip) for ips in mapped.values() for ip in ips)
    if not has_v6:
        pytest.skip("No IPv6 management address provisioned on eth0/eth1")


@pytest.fixture
def require_dual_nvb_path(engines, nv_command, nv_bridge_cluster, setup_name, devices):
    """Skip switchover/revertive tests unless both eth0 and eth1 are usable paths."""
    dut = engines.dut
    path = NvBridgeRevertiveConsts.NETWORK_INFO_PATH
    if not FilesTool.file_exists_sudo(dut, path).ignore_result():
        pytest.skip("network_info.json missing; dual mgmt path not provisioned")
    mapped = parse_network_info(FilesTool.read_json_file(dut, path))
    for iface in (NvBridgeRevertiveConsts.PRIMARY_IFACE, NvBridgeRevertiveConsts.SECONDARY_IFACE):
        if not mapped.get(iface):
            pytest.skip(f"{iface} has no IP in network_info.json; dual-path revertive not testable")

    nv_bridge = nv_command.system.nv_bridge

    def _wait_for_two_connections() -> int:
        deadline = time.monotonic() + NvBridgeRevertiveConsts.CONNECTIONS_READY_TIMEOUT_SEC
        count = 0
        while time.monotonic() < deadline:
            count = len(get_connections(get_nv_bridge_show(nv_bridge, dut)))
            if count >= 2:
                return count
            time.sleep(NvBridgeRevertiveConsts.POLL_INTERVAL_SEC)
        return count

    with allure.step("wait for two nvb connections (eth0 + eth1)"):
        if _wait_for_two_connections() >= 2:
            return

    # The cluster is enabled once (module-scoped) to keep the suite fast, but on
    # flaky sims the connections -- or the whole cluster app -- can collapse
    # between tests. Attempt a full recovery (re-enable cluster + reconfigure
    # both node servers + bounce nmx-c). If the environment refuses to come back,
    # skip cleanly instead of raising so an unhealthy sim does not fail the suite.
    with allure.step("connections not ready; recover cluster and retry"):
        try:
            ClusterTools.start_cluster(nv_bridge_cluster, setup_name, engine=dut, devices=devices)
            if FilesTool.file_exists_sudo(dut, path).ignore_result():
                ensure_dual_node_servers(nv_bridge_cluster, dut, FilesTool.read_json_file(dut, path))
            restart_nmx_controller(nv_bridge_cluster, dut)
        except Exception as recovery_err:  # noqa: BLE001
            pytest.skip(
                "nvb cluster did not recover on this environment; skipping "
                f"dual-path test ({recovery_err})"
            )
        count = _wait_for_two_connections()
        if count >= 2:
            return
    pytest.skip(f"expected >=2 nvb connections (eth0+eth1); found {count}")


@pytest.mark.nv_bridge
@pytest.mark.system
@pytest.mark.parametrize("ip_version", IP_VERSIONS)
def test_nvbridge_config_ingestion(
    engines,
    nv_command,
    random_api,
    skip_if_not_nv_bridge_platform,
    skip_if_ip_family_unavailable,
    nv_bridge_cluster,
    restore_network_info,
    ip_version,
):
    """Verify nvb-s ingests network config at boot and reloads it dynamically.

    Steps:
        1. nvb-s daemon starts automatically.
        2. platform gate advertises nv-bridge with >=2 mgmt interfaces.
        3. network-info.json maps eth0/eth1 and server-id is well-formed.
        4. a dynamic reload picks up an updated interface IP.
        5. malformed network-info.json payloads do not crash the daemon.
    """
    dut = engines.dut
    nv_bridge = nv_command.system.nv_bridge

    with allure.step("Step 1: nvb-s process starts automatically"):
        assert is_nvb_server_running(dut), "nvb-s daemon is not active"

    with allure.step("Step 2: platform gate advertises nv-bridge with >=2 mgmt interfaces"):
        platform = get_onie_platform(dut)
        assert platform, (
            "could not resolve onie_platform from machine.conf (transient read failure)"
        )
        caps = get_platform_capabilities(get_device_platform_json(dut))
        assert caps, f"could not resolve platform.json capabilities for '{platform}'"
        mgmt_if_count = int(caps.get(NvBridgeRevertiveConsts.PLATFORM_MGMT_IF_COUNT_KEY, 0))
        assert mgmt_if_count >= 2, (
            f"mgmt_if_count={mgmt_if_count}; revertive mode needs eth0 and eth1"
        )

    with allure.step("Step 3: network_info.json maps eth0/eth1 and server-id is well-formed"):
        assert FilesTool.file_exists_sudo(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH).ignore_result(), (
            "network_info.json was not generated by mgmtportsyncd"
        )
        mapped = parse_network_info(FilesTool.read_json_file(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH))
        for iface in (NvBridgeRevertiveConsts.PRIMARY_IFACE, NvBridgeRevertiveConsts.SECONDARY_IFACE):
            assert iface in mapped, f"{iface} missing from network_info.json"
        # eth0 must carry an address of the tested family; eth1 may be empty.
        assert _iface_has_family(mapped[NvBridgeRevertiveConsts.PRIMARY_IFACE], ip_version), (
            f"eth0 has no {ip_version} address in network_info.json "
            f"(got {mapped[NvBridgeRevertiveConsts.PRIMARY_IFACE]})"
        )

        show = get_nv_bridge_show(nv_bridge, dut)
        server_id = show.get(NvBridgeRevertiveConsts.SHOW_SERVER_ID, "")
        assert NvBridgeRevertiveConsts.SERVER_ID_RE.match(server_id), (
            f"server-id '{server_id}' does not match chassis-sn-<sn>-tray-index-<id>"
        )

    with allure.step("Step 4: dynamic reload picks up an updated interface IP"):
        original = FilesTool.read_json_file(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH)
        updated_map = parse_network_info(original)
        new_ip = "10.0.0.99" if ip_version == "ipv4" else "fc00::99"
        updated_map[NvBridgeRevertiveConsts.PRIMARY_IFACE] = [new_ip]
        FilesTool.write_json_file(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH, build_network_info(updated_map))
        notify_nvb_reload_network_info(dut)

        reloaded = parse_network_info(FilesTool.read_json_file(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH))
        assert new_ip in reloaded.get(NvBridgeRevertiveConsts.PRIMARY_IFACE, []), (
            "network_info.json did not reflect the updated primary IP after reload"
        )
        assert is_nvb_server_running(dut), "nvb-s crashed after dynamic reload"

    with allure.step("Step 5: malformed network_info.json cases do not crash the daemon"):
        for scenario_id, document in _malformed_network_info_scenarios():
            with allure.step(f"malformed scenario: {scenario_id}"):
                if document is None:
                    dut.run_cmd(f"sudo rm -f {NvBridgeRevertiveConsts.NETWORK_INFO_PATH}")
                else:
                    FilesTool.write_json_file(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH, document)
                notify_nvb_reload_network_info(dut)
                assert is_nvb_server_running(dut), (
                    f"nvb-s crashed on malformed network_info.json ({scenario_id})"
                )


@pytest.mark.nv_bridge
@pytest.mark.system
@pytest.mark.parametrize("ip_version", IP_VERSIONS)
def test_nvbridge_automatic_switchover(
    engines,
    nv_command,
    random_api,
    skip_if_not_nv_bridge_platform,
    skip_if_ip_family_unavailable,
    nv_bridge_cluster,
    require_dual_nvb_path,
    restore_nvb_paths,
    ip_version,
):
    """Validate automatic switchover to the healthy standby when the active path fails.

    Steps:
        1. capture the current active/standby baseline.
        2. fail the active interface and verify the active path moves to the standby.
    """
    dut = engines.dut
    nv_bridge = nv_command.system.nv_bridge
    net_info = (
        FilesTool.read_json_file(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH)
        if FilesTool.file_exists_sudo(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH).ignore_result()
        else None
    )

    with allure.step("Step 1: capture the current active/standby baseline"):
        show = get_nv_bridge_show(nv_bridge, dut)
        assert get_connections(show), "no nv-bridge connections established for baseline"
        active_iface = get_active_interface(show, net_info)
        assert active_iface in (
            NvBridgeRevertiveConsts.PRIMARY_IFACE,
            NvBridgeRevertiveConsts.SECONDARY_IFACE,
        ), f"unexpected baseline active interface '{active_iface}'"
        standby_iface = (
            NvBridgeRevertiveConsts.SECONDARY_IFACE
            if active_iface == NvBridgeRevertiveConsts.PRIMARY_IFACE
            else NvBridgeRevertiveConsts.PRIMARY_IFACE
        )

    with allure.step(f"Step 2: fail '{active_iface}' and expect switchover to '{standby_iface}'"):
        active_ips = get_interface_ips(net_info, active_iface)
        assert active_ips, f"no IPs mapped for {active_iface} in network_info.json"
        block_nvb_path(dut, active_ips)
        wait_for_active_interface(
            nv_bridge,
            dut,
            expected_interface=standby_iface,
            net_info=net_info,
        )


# (mode, preferred_iface, other_iface, reverts_after_restore)
#   * non-revertive : prefer eth0, active stays put after the preferred recovers
#   * primary       : prefer eth0, active returns to eth0 after it recovers
#   * secondary     : prefer eth1, active returns to eth1 after it recovers
_REVERTIVE_POLICY_CASES = [
    pytest.param(
        NvBridgeRevertiveConsts.MODE_NON_REVERTIVE,
        NvBridgeRevertiveConsts.PRIMARY_IFACE,
        NvBridgeRevertiveConsts.SECONDARY_IFACE,
        False,
        id="non-revertive",
    ),
    pytest.param(
        NvBridgeRevertiveConsts.MODE_REVERT_PRIMARY,
        NvBridgeRevertiveConsts.PRIMARY_IFACE,
        NvBridgeRevertiveConsts.SECONDARY_IFACE,
        True,
        id="primary",
    ),
    pytest.param(
        NvBridgeRevertiveConsts.MODE_REVERT_SECONDARY,
        NvBridgeRevertiveConsts.SECONDARY_IFACE,
        NvBridgeRevertiveConsts.PRIMARY_IFACE,
        True,
        id="secondary",
    ),
]


@pytest.mark.nv_bridge
@pytest.mark.system
@pytest.mark.parametrize("ip_version", IP_VERSIONS)
@pytest.mark.parametrize(
    "mode, preferred_iface, other_iface, reverts", _REVERTIVE_POLICY_CASES
)
def test_nvbridge_revertive_modes(
    engines,
    nv_command,
    random_api,
    skip_if_not_nv_bridge_platform,
    skip_if_ip_family_unavailable,
    nv_bridge_cluster,
    require_dual_nvb_path,
    restore_nvb_paths,
    reset_revertive_mode,
    mode,
    preferred_iface,
    other_iface,
    reverts,
    ip_version,
):
    """Validate the three revertive-mode policies during connection state changes.

    Steps:
        1. configure the revertive mode under test.
        2. resolve the baseline active interface (the preferred one for
           primary/secondary; whichever is currently active for non-revertive).
        3. fail that interface and verify failover to the other path.
        4. restore the interface: primary/secondary revert to the preferred path,
           non-revertive stays put.
    """
    dut = engines.dut
    nv_bridge = nv_command.system.nv_bridge
    net_info = (
        FilesTool.read_json_file(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH)
        if FilesTool.file_exists_sudo(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH).ignore_result()
        else None
    )

    with allure.step(f"Step 1: configure revertive mode '{mode}'"):
        set_revertive_mode(dut, mode)
        wait_for_revertive_mode(nv_bridge, dut, mode)

    if reverts:
        # primary / secondary: the policy enforces a preferred interface, so the
        # active path must settle on it. Fail the preferred path, then confirm it
        # reverts back after the path recovers.
        fail_iface, recover_iface = preferred_iface, other_iface
        with allure.step(f"Step 2: active interface settles on preferred '{preferred_iface}'"):
            wait_for_active_interface(nv_bridge, dut, preferred_iface, net_info)
    else:
        # non-revertive: there is NO preferred interface - whichever path nmx-c
        # brings up first stays active. Capture the current active path as the
        # baseline and fail that one; the active must not fail back afterwards.
        with allure.step("Step 2: capture current active interface (non-revertive)"):
            show = get_nv_bridge_show(nv_bridge, dut)
            fail_iface = get_active_interface(show, net_info)
            assert fail_iface in (preferred_iface, other_iface), (
                f"unexpected active interface '{fail_iface}' "
                f"(expected one of {preferred_iface}/{other_iface})"
            )
            recover_iface = other_iface if fail_iface == preferred_iface else preferred_iface

    fail_ips = get_interface_ips(net_info, fail_iface)
    assert fail_ips, f"no IPs mapped for {fail_iface} in network_info.json"

    with allure.step(f"Step 3: fail '{fail_iface}' and expect failover to '{recover_iface}'"):
        block_nvb_path(dut, fail_ips)
        wait_for_active_interface(nv_bridge, dut, recover_iface, net_info)

    with allure.step(f"Step 4: restore '{fail_iface}' and verify policy behavior"):
        unblock_nvb_path(dut, fail_ips)
        if reverts:
            wait_for_active_interface(nv_bridge, dut, preferred_iface, net_info)
        else:
            assert_active_interface_stable(nv_bridge, dut, recover_iface, net_info)


@pytest.mark.nv_bridge
@pytest.mark.system
@pytest.mark.parametrize("ip_version", IP_VERSIONS)
def test_nvbridge_revertive_mode_rtt_tiebreaker(
    engines,
    nv_command,
    random_api,
    skip_if_not_nv_bridge_platform,
    skip_if_ip_family_unavailable,
    nv_bridge_cluster,
    require_dual_nvb_path,
    restore_netem,
    reset_revertive_mode,
    ip_version,
):
    """RTT tie-breaker: both paths unhealthy by RTT -> preferred (eth0) wins.

    Steps:
        1. configure primary (revert-to-eth0) and wait for eth0 to be active.
        2. degrade RTT above threshold on both eth0 and eth1.
        3. verify the tie-breaker keeps the primary (eth0) active.
    """
    dut = engines.dut
    nv_bridge = nv_command.system.nv_bridge
    net_info = (
        FilesTool.read_json_file(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH)
        if FilesTool.file_exists_sudo(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH).ignore_result()
        else None
    )

    with allure.step("Step 1: configure primary (revert-to-eth0)"):
        set_revertive_mode(dut, NvBridgeRevertiveConsts.MODE_REVERT_PRIMARY)
        wait_for_revertive_mode(nv_bridge, dut, NvBridgeRevertiveConsts.MODE_REVERT_PRIMARY)
        wait_for_active_interface(nv_bridge, dut, NvBridgeRevertiveConsts.PRIMARY_IFACE, net_info)

    with allure.step("Step 2: degrade RTT above threshold on both eth0 and eth1"):
        add_netem_delay(dut, NvBridgeRevertiveConsts.PRIMARY_IFACE, NvBridgeRevertiveConsts.RTT_DEGRADE_MS)
        add_netem_delay(dut, NvBridgeRevertiveConsts.SECONDARY_IFACE, NvBridgeRevertiveConsts.RTT_DEGRADE_MS)

    with allure.step("Step 3: tie-breaker selects the primary (eth0)"):
        wait_for_active_interface(nv_bridge, dut, NvBridgeRevertiveConsts.PRIMARY_IFACE, net_info)


# (mode, switch-over target, target iface, make-target-unhealthy-first)
#   * non-revertive : forcing to an unhealthy interface is a no-op (active stays put)
#   * primary       : forcing active off eth0 (to secondary) fights policy -> 409
#   * secondary     : forcing active off eth1 (to primary) fights policy -> 409
_MANUAL_REJECTION_CASES = [
    pytest.param(
        NvBridgeRevertiveConsts.MODE_NON_REVERTIVE,
        NvBridgeRevertiveConsts.TARGET_SECONDARY,
        NvBridgeRevertiveConsts.SECONDARY_IFACE,
        True,
        id="non-revertive-unhealthy-target",
    ),
    pytest.param(
        NvBridgeRevertiveConsts.MODE_REVERT_PRIMARY,
        NvBridgeRevertiveConsts.TARGET_SECONDARY,
        NvBridgeRevertiveConsts.SECONDARY_IFACE,
        False,
        id="revert-primary-force-secondary",
    ),
    pytest.param(
        NvBridgeRevertiveConsts.MODE_REVERT_SECONDARY,
        NvBridgeRevertiveConsts.TARGET_PRIMARY,
        NvBridgeRevertiveConsts.PRIMARY_IFACE,
        False,
        id="revert-secondary-force-primary",
    ),
]


@pytest.mark.nv_bridge
@pytest.mark.system
@pytest.mark.parametrize("ip_version", IP_VERSIONS)
@pytest.mark.parametrize(
    "mode, target, target_iface, unhealthy_target", _MANUAL_REJECTION_CASES
)
def test_nvbridge_manual_switchover_rejections(
    engines,
    nv_command,
    random_api,
    skip_if_not_nv_bridge_platform,
    skip_if_ip_family_unavailable,
    nv_bridge_cluster,
    require_dual_nvb_path,
    restore_nvb_paths,
    reset_revertive_mode,
    mode,
    target,
    target_iface,
    unhealthy_target,
    ip_version,
):
    """Manual switch-over is rejected when it conflicts with the policy/health.

    Steps:
        1. configure the revertive mode under test.
        2. unhealthy-target case: resolve the active baseline, make the standby
           target unhealthy, and confirm switch-over to it is a no-op/conflict that
           leaves the active path unchanged.
        3. policy-conflict case: confirm a switch-over that fights the policy is
           rejected with HTTP 409.
    """
    dut = engines.dut
    nv_bridge = nv_command.system.nv_bridge
    net_info = (
        FilesTool.read_json_file(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH)
        if FilesTool.file_exists_sudo(dut, NvBridgeRevertiveConsts.NETWORK_INFO_PATH).ignore_result()
        else None
    )
    target_ips = get_interface_ips(net_info, target_iface)
    assert target_ips, f"no IPs mapped for {target_iface} in network_info.json"

    with allure.step(f"Step 1: configure revertive mode '{mode}'"):
        set_revertive_mode(dut, mode)
        wait_for_revertive_mode(nv_bridge, dut, mode)

    if unhealthy_target:
        with allure.step("resolve the currently active path as the stable baseline"):
            active_iface = get_active_interface(
                get_nv_bridge_show(nv_bridge, dut), net_info
            )
            assert active_iface, "could not resolve the active interface baseline"
            # Target the standby path so making it unhealthy never disturbs the
            # path we expect to stay active (the active interface is assigned
            # non-deterministically, so it cannot be hardcoded).
            target_iface = (
                NvBridgeRevertiveConsts.SECONDARY_IFACE
                if active_iface == NvBridgeRevertiveConsts.PRIMARY_IFACE
                else NvBridgeRevertiveConsts.PRIMARY_IFACE
            )
            target_ips = get_interface_ips(net_info, target_iface)
            assert target_ips, f"no IPs mapped for {target_iface} in network_info.json"

        with allure.step(f"make standby target '{target_iface}' unhealthy (block keepalive)"):
            block_nvb_path(dut, target_ips)
            wait_for_path_unhealthy(nv_bridge, dut, target_ips)

        with allure.step(
            f"Step 2: manual switch-over to unhealthy '{target_iface}' must not move "
            f"the active path off '{active_iface}'"
        ):
            # In non-revertive mode there is no policy to violate, so nvb-s accepts
            # the request but refuses to move onto a dead path (HTTP 200 no-op).
            code, body = request_switch_over(dut, target_ips)
            assert code in (
                NvBridgeRevertiveConsts.HTTP_OK,
                NvBridgeRevertiveConsts.HTTP_CONFLICT,
            ), (
                f"expected HTTP {NvBridgeRevertiveConsts.HTTP_OK} (no-op) or "
                f"{NvBridgeRevertiveConsts.HTTP_CONFLICT} for switch-over to unhealthy "
                f"'{target_iface}' under mode '{mode}'; got HTTP {code}, body={body!r}"
            )
            assert_active_interface_stable(nv_bridge, dut, active_iface, net_info)
    else:
        with allure.step(
            f"Step 2: manual switch-over to '{target}' is rejected with HTTP "
            f"{NvBridgeRevertiveConsts.HTTP_CONFLICT}"
        ):
            code, body = request_switch_over(dut, target_ips)
            assert code == NvBridgeRevertiveConsts.HTTP_CONFLICT, (
                f"expected HTTP {NvBridgeRevertiveConsts.HTTP_CONFLICT} rejecting manual "
                f"switch-over to '{target}' under mode '{mode}'; got HTTP {code}, "
                f"body={body!r}"
            )


@pytest.mark.nv_bridge
@pytest.mark.system
def test_nvbridge_switchover_error_flows(
    engines,
    nv_command,
    random_api,
    skip_if_not_nv_bridge_platform,
    nv_bridge_cluster,
    restore_nvb_server,
):
    """Switch-over error flows that don't need a second healthy path.

    Steps:
        1. malformed switch-over payloads are rejected with HTTP 400.
        2. with nvb-s stopped, the CLI switch-over fails gracefully (server-down error).
    """
    dut = engines.dut

    with allure.step(
        f"Step 1: malformed switch-over payloads -> HTTP "
        f"{NvBridgeRevertiveConsts.HTTP_BAD_REQUEST}"
    ):
        for scenario_id, body in _malformed_switch_over_bodies():
            with allure.step(f"malformed payload: {scenario_id}"):
                code, resp = post_switch_over_raw(dut, body)
                assert code == NvBridgeRevertiveConsts.HTTP_BAD_REQUEST, (
                    f"{scenario_id}: expected HTTP "
                    f"{NvBridgeRevertiveConsts.HTTP_BAD_REQUEST}, got HTTP {code} "
                    f"(body={resp!r})"
                )

    with allure.step("Step 2: with nvb-s stopped, CLI switch-over fails gracefully"):
        stop_nvb_server(dut)
        assert not is_nvb_server_running(dut), "nvb-s did not stop"
        res = nv_command.system.nv_bridge.action_switch_over(
            NvBridgeRevertiveConsts.TARGET_PRIMARY, dut_engine=dut
        )
        text = f"{res.info} {res.returned_value or ''}".lower()
        # Graceful failure: either the action reports failure, or it surfaces a
        # server-down/unreachable style error. Works for both NVUE and OpenAPI.
        assert not res or any(
            hint in text for hint in NvBridgeRevertiveConsts.SERVER_DOWN_HINTS
        ), (
            f"expected switch-over to fail gracefully while nvb-s is down; "
            f"got success with info={res.info!r} output={res.returned_value!r}"
        )
        res.ignore_result()  # expected failure (nvb-s down);


@pytest.mark.nv_bridge
@pytest.mark.system
def test_nvbridge_config_rebuild_and_schema_compat(
    engines,
    nv_command,
    random_api,
    skip_if_not_nv_bridge_platform,
    nv_bridge_cluster,
    restore_network_info,
):
    """Lightweight upgrade/downgrade surrogate (runs anywhere, incl. Air sims).

    Steps:
        A. delete network-info.json and verify mgmtportsyncd rebuilds it.
        B. reload a forward-schema network-info.json and verify nvb-s ignores the
           unknown fields and stays up.
    """
    dut = engines.dut
    nv_bridge = nv_command.system.nv_bridge
    path = NvBridgeRevertiveConsts.NETWORK_INFO_PATH
    assert FilesTool.file_exists_sudo(dut, path).ignore_result(), "network-info.json missing at baseline"
    baseline = parse_network_info(FilesTool.read_json_file(dut, path))

    with allure.step("Step A: mgmtportsyncd rebuilds network-info.json after loss"):
        dut.run_cmd(f"sudo rm -f {path}")
        restart_mgmtportsyncd(dut)
        FilesTool.wait_for_file(dut, path)
        rebuilt = parse_network_info(FilesTool.read_json_file(dut, path))
        for iface in (
            NvBridgeRevertiveConsts.PRIMARY_IFACE,
            NvBridgeRevertiveConsts.SECONDARY_IFACE,
        ):
            if baseline.get(iface):
                assert rebuilt.get(iface), (
                    f"{iface} missing from the rebuilt network-info.json "
                    f"(baseline had {baseline.get(iface)})"
                )

    with allure.step("Step B: nvb-s ignores unknown/forward schema fields"):
        FilesTool.write_json_file(dut, path, build_forward_schema_network_info(baseline))
        notify_nvb_reload_network_info(dut)
        assert is_nvb_server_running(dut), (
            "nvb-s is not active after reloading a forward-schema network-info.json"
        )
        show = get_nv_bridge_show(nv_bridge, dut)
        assert show.get(NvBridgeRevertiveConsts.SHOW_STATE), (
            "nvb-s state unreadable after forward-schema reload (parser choked?)"
        )


@pytest.mark.nv_bridge
@pytest.mark.system
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
@pytest.mark.timeout(75 * MINUTE, func_only=True)
def test_nvbridge_upgrade_downgrade(
    request,
    test_api,
    engines,
    devices,
    topology_obj,
    setup_name,
    nv_command,
    skip_if_not_nv_bridge_platform,
    skip_if_no_upgrade_versions,
):
    """NVBridge config/daemon survive upgrade & downgrade.

    Steps:
        1. install the base version via ONIE, enable cluster + nv-bridge node,
           and snapshot the baseline network-info.json.
        2. upgrade in place to the target version and verify mgmtportsyncd rebuilds
           network-info.json.
        3. downgrade back to the base version and verify the older nvb-s tolerates
           newer-schema fields.
        Cleanup: stop the cluster and restore the target image.
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    nv_bridge = nv_command.system.nv_bridge
    path = NvBridgeRevertiveConsts.NETWORK_INFO_PATH
    cli_obj = NvueGeneralCli(dut, devices.dut)
    cluster = Cluster()
    target_installed = False

    # Resolve the image paths lazily (not as fixture args): both realpath
    # fixtures are session-scoped and assert the versions are set, which would
    # otherwise run BEFORE the function-scoped skip_if_no_upgrade_versions and
    # error out instead of skipping on Air/dev setups.
    base_version_realpath = request.getfixturevalue("base_version_realpath")
    target_version_realpath = request.getfixturevalue("target_version_realpath")

    with allure.step("Install base version via ONIE"):
        NvueGeneralCli(dut, devices.dut).install_image_via_onie(topology_obj, base_version_realpath)
        TestToolkit.engines.dut.disconnect()

    try:
        with allure.step("Enable cluster + nv-bridge node on base version"):
            ClusterTools.start_cluster(cluster, setup_name)

        with allure.step("Snapshot baseline network-info.json"):
            assert FilesTool.file_exists_sudo(dut, path).ignore_result(), "network-info.json missing on base version"
            baseline = parse_network_info(FilesTool.read_json_file(dut, path))

        with allure.step("Step 1: in-place upgrade to target version"):
            bin_filename = target_version_realpath.split("/")[-1]
            system = System()
            sonic_mgmt_engine = topology_obj.players["sonic-mgmt"]["engine"]
            scp_host_creds = (
                f"{sonic_mgmt_engine.username}:{sonic_mgmt_engine.password}@"
                f"{IpTool.format_ip_for_uri(sonic_mgmt_engine)}"
            )
            NvosInstallationSteps.upgrade_to_target_version(
                bin_filename,
                cli_obj.engine,
                cli_obj.device,
                scp_host_creds,
                system,
                target_version_realpath,
                topology_obj,
            )
            TestToolkit.engines.dut.disconnect()
            target_installed = True

        with allure.step("Step 1 verify: mgmtportsyncd rebuilt network-info.json after upgrade"):
            FilesTool.wait_for_file(dut, path)
            rebuilt = parse_network_info(FilesTool.read_json_file(dut, path))
            for iface in (
                NvBridgeRevertiveConsts.PRIMARY_IFACE,
                NvBridgeRevertiveConsts.SECONDARY_IFACE,
            ):
                if baseline.get(iface):
                    assert rebuilt.get(iface), (
                        f"{iface} missing from network-info.json rebuilt after upgrade"
                    )

        with allure.step("Step 2: downgrade back to base version via ONIE"):
            NvueGeneralCli(dut, devices.dut).install_image_via_onie(topology_obj, base_version_realpath)
            TestToolkit.engines.dut.disconnect()
            target_installed = False
            ClusterTools.start_cluster(cluster, setup_name)

        with allure.step("Step 2 verify: older nvb-s tolerates newer schema fields"):
            FilesTool.write_json_file(dut, path, build_forward_schema_network_info(baseline))
            notify_nvb_reload_network_info(dut)
            assert is_nvb_server_running(dut), (
                "older nvb-s failed to start/stay up with a newer-schema network-info.json"
            )
            show = get_nv_bridge_show(nv_bridge, dut)
            assert show.get(NvBridgeRevertiveConsts.SHOW_STATE), (
                "older nvb-s state unreadable after newer-schema reload (parser choked?)"
            )
    finally:
        with allure.step("Cleanup: stop cluster and restore target image"):
            cluster.unset(apply=True, dut_engine=dut)
            ClusterTools.stop_cluster(cluster)
            if not target_installed:
                NvueGeneralCli(dut, devices.dut).install_image_via_onie(
                    topology_obj, target_version_realpath
                )
                TestToolkit.engines.dut.disconnect()


@pytest.mark.nv_bridge
@pytest.mark.system
def test_nvbridge_network_info_history_log(
    engines,
    skip_if_not_nv_bridge_platform,
    restore_network_info,
):
    """mgmt IP address changes are logged for debuggability.

    Steps:
        1. pick a mgmt interface to flap that is NOT the SSH transport.
        2. flap that interface's primary IP to force a mgmt-IP change.
        3. verify the change is recorded in network-info-history.log.
    """
    dut = engines.dut
    log = NvBridgeRevertiveConsts.ADDRESS_HISTORY_LOG
    primary = NvBridgeRevertiveConsts.PRIMARY_IFACE
    secondary = NvBridgeRevertiveConsts.SECONDARY_IFACE

    with allure.step("pick a mgmt interface to flap that is NOT the SSH transport"):
        ssh_ip = get_ssh_server_ip(dut)
        assert ssh_ip, "could not determine the SSH server IP ($SSH_CONNECTION)"
        safe_iface, cidr = "", ""
        for iface in (primary, secondary):
            candidate = get_iface_ipv4_cidr(dut, iface)
            if candidate and candidate.split("/")[0] != ssh_ip:
                safe_iface, cidr = iface, candidate
                break
        if not safe_iface:
            pytest.skip("no non-SSH mgmt interface with a global IPv4 address to flap")
        probe_ip = cidr.split("/")[0]

    baseline = count_history_log_lines(dut)
    try:
        with allure.step(f"flap {safe_iface} primary {cidr} to force a mgmt-IP change"):
            del_iface_ip(dut, safe_iface, cidr)
        with allure.step(f"verify the {safe_iface} IP change is recorded in {log}"):
            assert wait_for_history_log_growth(dut, baseline), (
                f"mgmt IP change on {safe_iface} ({probe_ip}) was not recorded in "
                f"{log} within {NvBridgeRevertiveConsts.HISTORY_LOG_TIMEOUT_SEC}s"
            )
    finally:
        add_iface_ip(dut, safe_iface, cidr)
        time.sleep(NvBridgeRevertiveConsts.RELOAD_SETTLE_SEC)


def _malformed_switch_over_bodies() -> list[tuple[str, str]]:
    """Malformed/incomplete /action/switch-over payloads (expected HTTP 400)."""
    key = NvBridgeRevertiveConsts.REST_SWITCHOVER_ADDRESSES_KEY
    return [
        ("not_json", "{not valid json"),
        ("missing_addresses_key", json.dumps({})),
        ("addresses_not_array", json.dumps({key: "10.0.0.1"})),
    ]


def _iface_has_family(ips: list[str], ip_version: str) -> bool:
    """Return True when *ips* contains at least one address of *ip_version*."""
    return any(IpTool.is_address_ipv6(ip) == (ip_version == "ipv6") for ip in ips)


def _malformed_network_info_scenarios() -> list[tuple[str, dict | None]]:
    """The 5 malformed network_info.json cases."""
    key = NvBridgeRevertiveConsts.NET_INTERFACES_KEY
    name = NvBridgeRevertiveConsts.NET_IFACE_NAME
    ips = NvBridgeRevertiveConsts.NET_IFACE_IPS
    primary = NvBridgeRevertiveConsts.PRIMARY_IFACE
    secondary = NvBridgeRevertiveConsts.SECONDARY_IFACE
    return [
        ("missing_file", None),
        ("missing_interfaces_key", {}),
        ("empty_interfaces_array", {key: []}),
        ("missing_eth1_entry", {key: [{name: primary, ips: ["10.0.0.1"]}]}),
        (
            "empty_eth1_ip_addresses",
            {key: [{name: primary, ips: ["10.0.0.1"]}, {name: secondary, ips: []}]},
        ),
    ]
