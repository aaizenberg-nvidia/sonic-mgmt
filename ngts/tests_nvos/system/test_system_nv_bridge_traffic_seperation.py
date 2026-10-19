import random
import string
import tempfile
import time
from functools import partial

from scapy.all import rdpcap
from scapy.layers.inet import ICMP, IP, TCP, UDP
from scapy.layers.inet6 import IPv6

from ngts.ngts_types import EnginesT, TopologyT, CleanUpT
from ngts.nvos_tools.infra import IpTool
from ngts.nvos_tools.system import System
from ngts.nvos_tools.ib.InterfaceConfiguration import Port
from ngts.tests_nvos.helpers.interfaces.mgmt_traffic_capture import serial_capture_interface_traffic
from ngts.nvos_tools.infra.TcpdumpCmdBuilder import TcpdumpCmdBuilder
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_constants.constants_nvos import NvosConst, SystemConsts
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine
from ngts.constants.constants import GnmiConsts
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import GnmiMode, GnmicErr
from ngts.tests_nvos.system.gnmi.helpers import verify_msg_not_in_out_or_err
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.tests_nvos.system.syslog.test_system_syslog import verify_msg_in_syslog_file

GNMI_TESTED_INTERFACE = 'acp225'
NVBRIDGE_PORT = 50052
MGMT_TCP_PORTS = (22, 9339)    # SSH, gNMI
MGMT_UDP_PORTS = (5514,)       # remote syslog


def _analyze_traffic_separation(pcap_path: str, max_examples: int = 5) -> dict:
    """
    Verify only NVBridge (TCP 50052) uses IPv4 in the capture, and management
    traffic (SSH/gNMI/syslog) rides IPv6. Any other IPv4 packet is unexpected;
    non-IP packets are ignored.
    """
    nvbridge_ipv4_count = mgmt_ipv6_count = unexpected_ipv4_count = 0
    unexpected_ipv4_examples = []

    for pkt in rdpcap(pcap_path):
        if pkt.haslayer(IP):
            if pkt.haslayer(TCP) and NVBRIDGE_PORT in (pkt[TCP].sport, pkt[TCP].dport):
                nvbridge_ipv4_count += 1
                continue

            unexpected_ipv4_count += 1
            if pkt.haslayer(TCP):
                proto, sport, dport = "TCP", pkt[TCP].sport, pkt[TCP].dport
            elif pkt.haslayer(UDP):
                proto, sport, dport = "UDP", pkt[UDP].sport, pkt[UDP].dport
            elif pkt.haslayer(ICMP):
                proto, sport, dport = "ICMP", None, None
            else:
                proto, sport, dport = "other", None, None
            if len(unexpected_ipv4_examples) < max_examples:
                unexpected_ipv4_examples.append(
                    {"src": pkt[IP].src, "dst": pkt[IP].dst, "proto": proto, "sport": sport, "dport": dport})

        elif pkt.haslayer(IPv6):
            if pkt.haslayer(TCP) and (pkt[TCP].sport in MGMT_TCP_PORTS or pkt[TCP].dport in MGMT_TCP_PORTS):
                mgmt_ipv6_count += 1
            elif pkt.haslayer(UDP) and (pkt[UDP].sport in MGMT_UDP_PORTS or pkt[UDP].dport in MGMT_UDP_PORTS):
                mgmt_ipv6_count += 1
        # non-IP packets (ARP, NDP, ...) are ignored - not needed for the verdict.

    return {
        "nvbridge_ipv4_count": nvbridge_ipv4_count,
        "mgmt_ipv6_count": mgmt_ipv6_count,
        "unexpected_ipv4_count": unexpected_ipv4_count,
        "unexpected_ipv4_examples": unexpected_ipv4_examples,
        "verdict": "PASS" if unexpected_ipv4_count == 0 else "FAIL",
    }


def _restore_traffic_separation_state(eth0_port: Port.Port, system: System.System, dut_engine: LinuxSshEngine) -> None:
    """
    Undo everything the nvbridge/management traffic separation test configures: syslog server,
    cluster/nvbridge node list, and eth0 IPv4/IPv6 addressing. Bounces the IPv4 DHCP
    client so eth0 reacquires both its address and default gateway, restoring
    engines.dut reachability for later tests instead of leaving it on a static,
    gateway-less IPv4 address.
    """
    with allure.step("Cleanup: restore syslog, cluster, and eth0 IPv4/IPv6 addressing"):
        system.syslog.unset(apply=True, dut_engine=dut_engine)
        Cluster().unset(apply=True, dut_engine=dut_engine)
        eth0_port.interface.ipv6.address.unset(dut_engine=dut_engine)
        eth0_port.interface.ipv6.gateway.unset(dut_engine=dut_engine)
        eth0_port.interface.ipv4.address.unset(dut_engine=dut_engine)
        eth0_port.interface.ipv4.dhcp_client.set(
            SystemConsts.STATE, NvosConst.DISABLED, dut_engine=dut_engine,
        ).verify_result()
        eth0_port.interface.ipv4.dhcp_client.set(
            SystemConsts.STATE, NvosConst.ENABLED, apply=True, dut_engine=dut_engine,
        ).verify_result()


def test_system_nv_bridge_traffic_seperation(
    engines: EnginesT,
    topology_obj: TopologyT,
    dut_engines: dict[str, LinuxSshEngine],
    serial_engine: PexpectSerialEngine,
    register_cleanup: CleanUpT,
):
    system: System.System = System.System()
    eth0_port: Port.Port = Port.Port("eth0")
    dut_setup_specific_attributes: dict[str, str] = topology_obj.players['dut']['attributes']\
        .noga_query_data['attributes']['Specific']
    self_eth0_ipv4_address: str = dut_setup_specific_attributes['ip_address']
    dut_eth1_connection: LinuxSshEngine = ConnectionTool.create_ssh_conn(
        ip=dut_setup_specific_attributes['ip_address_2'],
        username=engines.dut.username,
        password=engines.dut.password,
    ).get_returned_value()
    # register_cleanup finalizers run LIFO, so register disconnect first: the restore
    # step (registered below, runs first) still needs dut_eth1_connection open.
    register_cleanup(dut_eth1_connection.disconnect)
    register_cleanup(partial(_restore_traffic_separation_state, eth0_port, system, dut_eth1_connection))
    dut_eth0_ipv6_address: str = IpTool.IpTool.get_dut_ipv6_addr_of_given_eth_interface_using_nv_cli(
        "eth0",
        dut_eth1_connection,
    )
    dut_eth0_ipv6_gateway: str = next(iter(eth0_port.interface.ipv6.gateway.parse_show()), None)
    assert dut_eth0_ipv6_gateway is not None, "No IPv6 gateway found on eth0"

    with allure.step("Set eth0 static no gateway"):
        eth0_port.interface.ipv4.address.set(
            self_eth0_ipv4_address,
            dut_engine=dut_eth1_connection,
        ).verify_result()
        eth0_port.interface.ipv4.gateway.unset(
            dut_engine=dut_eth1_connection
        ).verify_result()

    with allure.step("Set static IPv6 no gateway"):
        eth0_port.interface.ipv6.address.set(
            dut_eth0_ipv6_address,
            dut_engine=dut_eth1_connection,
        ).verify_result()
        eth0_port.interface.ipv6.gateway.set(
            dut_eth0_ipv6_gateway,
            dut_engine=dut_eth1_connection,
        ).verify_result()

    cluster: Cluster = Cluster()
    dut, dut2 = random.sample(list(dut_engines.values()), k=2)

    with allure.step("Set remote nvbridge addresses in the same subnet with the eth0 IPv4 address"):
        cluster.node.primary.set(
            op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER,
            op_param_value=dut.ip,
            dut_engine=dut_eth1_connection,
        ).verify_result()
        cluster.node.primary.set(
            op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER,
            op_param_value=dut2.ip,
            dut_engine=dut_eth1_connection,
        ).verify_result()

    with allure.step("Run NMX-C"):
        cluster.set(
            op_param_name=SystemConsts.STATE,
            op_param_value=SystemConsts.CLUSTER_STATE_ENABLED,
            dut_engine=dut_eth1_connection,
        ).verify_result()

    with allure.step("Apply the config"):
        NvueGeneralCli.apply_config(dut_eth1_connection, ask_for_confirmation=True)

    with tempfile.NamedTemporaryFile(suffix=".pcap") as tmp:
        tmp.flush()
        with allure.step("Verify separation by operation"):
            with serial_capture_interface_traffic(
                serial_engine=serial_engine,
                dut_engine=dut_eth1_connection,
                tcpdump_cmd=TcpdumpCmdBuilder().sudo().interface("eth0"),
                local_file_path=tmp.name,
            ):
                with allure.step(f"Verify ssh connection and run id for ssh admin@{dut_eth0_ipv6_address}"):
                    dut_eth0_ipv6_connection: LinuxSshEngine = ConnectionTool.create_ssh_conn(
                        ip=dut_eth0_ipv6_address,
                        username=engines.dut.username,
                        password=engines.dut.password,
                    ).get_returned_value()
                    register_cleanup(dut_eth0_ipv6_connection.disconnect)
                    id_output: str = dut_eth0_ipv6_connection.run_cmd("id")
                    assert "uid=" in id_output, f"id command did not return expected output: {id_output}"
                with allure.step(f"Verify gNMI from external host to {dut_eth0_ipv6_address}"):
                    gnmi_client = GnmiClient(dut_eth0_ipv6_address, GnmiConsts.GNMI_DEFAULT_PORT,
                                             engines.dut.username, engines.dut.password,
                                             verify_tools_installed=True)
                    gnmi_out, gnmi_err = gnmi_client.gnmic_subscribe_interface(
                        mode=GnmiMode.ONCE,
                        interface_name=GNMI_TESTED_INTERFACE,
                        interface_path='state/admin-status',
                        skip_cert_verify=True,
                        wait_till_done=True,
                    )
                    verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, gnmi_out, gnmi_err)
                    assert 'admin-status' in gnmi_out, \
                        f"gnmic subscribe did not return admin-status.\nout: {gnmi_out}\nerr: {gnmi_err}"
                with allure.step("Verify remote syslog to external host over IPv6"):
                    syslog_server_ipv6: str = IpTool.IpTool.get_player_ipv6_addr(engines.sonic_mgmt.ip, engines.sonic_mgmt)
                    system.syslog.servers.set(
                        op_param_name=syslog_server_ipv6,
                        op_param_value="",
                        apply=True,
                        dut_engine=dut_eth1_connection,
                    ).verify_result()
                    syslog_msg: str = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
                    time.sleep(3)
                    dut_eth1_connection.run_cmd(f'logger "{syslog_msg}"')
                    time.sleep(2)
                    verify_msg_in_syslog_file(engines.sonic_mgmt, syslog_msg, should_find=True)
        tmp.flush()
        with allure.step("Verify separation by traffic"):
            report = _analyze_traffic_separation(tmp.name)
            allure.attach("traffic_separation_report", str(report))
            assert report["nvbridge_ipv4_count"] > 0, "No NVBridge IPv4 traffic was captured"
            assert report["mgmt_ipv6_count"] > 0, "No management IPv6 traffic was captured"
            assert report["verdict"] == "PASS", \
                f"Unexpected IPv4 traffic found: {report['unexpected_ipv4_examples']}"
