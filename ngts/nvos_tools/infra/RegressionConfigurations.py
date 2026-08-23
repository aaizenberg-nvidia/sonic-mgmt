import logging
import json
from typing import Dict, Tuple, List

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import LinkDetectionConsts, PlatformConsts
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.infra import ExceptionTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


MTVR_MAMBA_06_0 = '10.245.21.54'
MTVR_MAMBA_06_1 = '10.245.21.65'
MTVR_CROC_19_0 = '10.245.21.19'
MTVR_CROC_19_1 = '10.245.21.68'
MTVR_CROC_50_0 = '10.245.21.50'
MTVR_CROC_50_1 = '10.245.21.67'


class Configurations:
    # List of NDR ports in Crocodile (for setting connection-mode to ndr)
    ndr_ports = {"10.7.148.94": ['swA1p1', 'swA2p1'],
                 "10.7.148.95": ['swA1p1', 'swA2p1'],
                 "10.7.145.61": ['swA1p1', 'swA14p1'],
                 "10.7.145.62": ['swA1p1', 'swA14p1'],
                 }

    # List of all ports connected to traffic servers
    traffic_ports = {
        # Black Mamba
        "10.7.148.248": ['sw61p1', 'sw67p1'],
        "10.7.148.249": ['sw61p1', 'sw67p1'],

        # Black Mamba DGX
        "10.7.145.81": ['sw1p1', 'sw2p1'],
        "10.7.145.82": ['sw1p1', 'sw2p1'],

        # Crocodile
        "10.7.148.94": ['swA1p1', 'swA2p1'],
        "10.7.148.95": ['swA1p1', 'swA2p1'],
        "10.7.145.61": ['swA1p1', 'swA14p1'],
        "10.7.145.62": ['swA1p1', 'swA14p1'],

        # Gorilla
        "10.7.144.153": ['sw1p1', 'sw1p2'],
        "10.7.144.154": ['sw1p1', 'sw1p2'],
        "10.7.144.58": ['sw1p1', 'sw1p2', 'sw2p1'],

        # Taipan
        "10.7.145.34": ['sw14p1', 'sw43p1'],
        "10.7.145.39": ['sw14p1', 'sw43p1'],
    }

    juliet_systems_with_loopbox = ["NVOS_juliet_10_7_148_136", "NVOS_juliet_10_7_148_184",
                                   "NVOS_juliet_10_7_145_85", "NVOS_juliet_10_7_148_142", "NVOS_surrogate_10_7_145_54", "NVOS_rosalind_spil_1",
                                   "NVOS_rosalind_skt_1", "NVOS_rosalind_eb1_10", "NVOS_rosalind_eb2_2102", "NVOS_juliet_10_7_148_126",
                                   "NVOS_rosalind_eb1_2152", "NVOS_rosalind_eb2_164", "NVOS_rosalind_eb2_2124"]
    juliet_systems_with_loopbox = ["NVOS_surrogate_10_7_145_41", "NVOS_juliet_10_7_148_136", "NVOS_juliet_10_7_148_184",
                                   "NVOS_juliet_10_7_145_85", "NVOS_juliet_10_7_148_142", "NVOS_juliet_10_7_148_126"]

    non_standalone_systems = ['NVOS_juliet_10_7_148_148', 'NVOS_sws_rtf2_rosalind_198']

    doca_traffic_systems = ['NVOS_taipan_10_7_145_34', 'NVOS_bm_10_220_12_132']

    systems_with_wrong_shunt_resistor = ['NVOS_juliet_10_7_148_128', 'NVOS_juliet_10_7_148_144']

    systems_with_wrong_shunt_resistor = ['NVOS_juliet_10_7_148_128', 'NVOS_juliet_10_7_148_144']

    compute_nodes_per_system = {
        'NVOS_juliet_10_7_148_148': [{'ip_address': '10.7.34.145', 'username': 'nvidia', 'password': 'nvidia'},
                                     {'ip_address': '10.7.34.192', 'username': 'nvidia', 'password': 'nvidia'}],
        'NVOS_sws_rtf2_rosalind_198': [{'ip_address': '10.220.9.22', 'username': 'nvidia', 'password': 'nvidia'},
                                       {'ip_address': '10.220.9.23', 'username': 'nvidia', 'password': 'nvidia'}]}

    # Per-setup tray model for SDN tray admin-state tests (RegressionConfigurations.tray_topology).
    # Keys must match Noga/pytest setup_name exactly.
    # switch_nodes / compute_nodes: expected 'nv show sdn trays' split — since the 6623822
    # fix (0.8 >= 25.03.0722) the view lists ALL trays: switch trays with a real
    # maintenance-state, compute trays with 'N/A'.
    # Maintenance-state tests use SWITCH trays only — compute-tray admin state is not
    # released to customers (product decision 2026-08-05; update/restore on a compute tray
    # are rejected with 'Tray admin state is not supported for tray N'):
    #   - compute_nodes>0: only the local switch tray is used.
    #   - compute_nodes==0: every tray is a switch tray; any may be used (note: the only
    #     such setup today, rosalind-simx, is standalone and skips the suite entirely).
    tray_topology = {
        'NVOS_sws_rtf2_rosalind_198': {'switch_nodes': 1, 'compute_nodes': 2},
        'NVOS_2_rosalind-simx': {'switch_nodes': 2, 'compute_nodes': 0},
    }

    ports_to_disable = {'NVOS_juliet_10_7_148_148': ['acp17-20', 'acp69-72'],
                        'NVOS_sws_rtf2_rosalind_198': []}

    oberon_num_of_gpus = {'NVOS_juliet_10_7_148_148': '8',
                          'NVOS_sws_rtf2_rosalind_198': '8'}

    # Map IPs to their post-install commands
    post_install_commands = {
        ip: [
            'nv set acl ACL_MGMT_INBOUND_CP_DEFAULT rule 120 match ip recent-list hit-count 3000',
            'nv config apply -y'
        ]
        for ip in ["10.245.21.67", "10.7.148.160", "10.7.148.161", "10.7.145.61", "10.7.145.62"]
    }

    devices_missing_psus = {"10.7.148.138"}
    # WA for Redmine #4963780: mtvr-q3400-05 has missing fans and burned FAN5 sockets
    devices_with_missing_fans = {
        "10.245.23.5": {"FAN5/"},
        "10.245.21.64": {"FAN5/"}
    }
    devices_to_configure_ndr_ports = ndr_ports.keys()
    devices_requested_factory_reset = []  # ['10.7.148.248']

    default_conf = NvosConst.DEFAULT_CONFIG
    default_conf["interface"] = {
        "eth0-1": {
            "acl": {
                "ACL_MGMT_INBOUND_CP_DEFAULT": {
                    "inbound": {
                        "control-plane": {}
                    }
                },
                "ACL_MGMT_INBOUND_CP_DEFAULT_IPV6": {
                    "inbound": {
                        "control-plane": {}
                    }
                },
                "ACL_MGMT_INBOUND_DEFAULT": {
                    "inbound": {}
                },
                "ACL_MGMT_INBOUND_DEFAULT_IPV6": {
                    "inbound": {}
                },
                "ACL_MGMT_OUTBOUND_CP_DEFAULT": {
                    "outbound": {
                        "control-plane": {}
                    }
                },
                "ACL_MGMT_OUTBOUND_CP_DEFAULT_IPV6": {
                    "outbound": {
                        "control-plane": {}
                    }
                }
            },
            "type": "eth"
        },
        "lo": {
            "acl": {
                "ACL_LOOPBACK_INBOUND_CP_DEFAULT": {
                    "inbound": {
                        "control-plane": {}
                    }
                },
                "ACL_LOOPBACK_INBOUND_CP_DEFAULT_IPV6": {
                    "inbound": {
                        "control-plane": {}
                    }
                }
            },
            "type": "loopback"
        }
    }

    @staticmethod
    def get_regression_default_config(engine):
        try:
            if engine.ip in Configurations.devices_to_configure_ndr_ports:
                ndr_ports = ",".join(list(Configurations.ndr_ports[engine.ip]))
                Configurations.default_conf["interface"][ndr_ports] = \
                    {"link": {
                        "connection-mode": "ndr"
                    }, "type": "ib"}
        except BaseException:
            pass

        if engine.ip in Configurations.devices_missing_psus:
            if 'platform' not in Configurations.default_conf:
                Configurations.default_conf['platform'] = {}
            Configurations.default_conf['platform']['ps-redundancy'] = {
                PlatformConsts.PS_REDUNDANCY_POLICY: PlatformConsts.PS_REDUNDANCY_NO}

        return Configurations.default_conf


class RegressionConfigurations:

    @staticmethod
    def set_base_configurations(engine: LinuxSshEngine, apply=True):
        with allure.step('Set base configurations for device'):
            with allure.independent_step('Setting ps-redundancy if needed'):
                RegressionConfigurations.configure_ps_redundancy_policy(engine)
            with allure.independent_step('Setting connection-mode if needed'):
                RegressionConfigurations.configure_ports_to_legacy(engine=engine, apply=False)
            if apply:
                with allure.independent_step('Applying base configuration (if there is a diff)'):
                    general_cli = TestToolkit.GeneralApi[TestToolkit.tested_api]
                    config_diff = OutputParsingTool.parse_json_str_to_dictionary(general_cli.show_config(engine)
                                                                                 ).get_returned_value()
                    if config_diff:
                        general_cli.apply_config(engine=engine, option='-y', verify_execution=True)

    @staticmethod
    def configure_ps_redundancy_policy(engine: LinuxSshEngine):
        if engine.ip in Configurations.devices_missing_psus:
            Platform().ps_redundancy.set(PlatformConsts.PS_REDUNDANCY_POLICY, PlatformConsts.PS_REDUNDANCY_NO,
                                         dut_engine=engine)

    @staticmethod
    def configure_ports_to_legacy(engine, apply=True, throw_exception=True, wait_till_port_up=False):
        try:
            if engine.ip in Configurations.devices_to_configure_ndr_ports:
                with allure.step("Updating connection mode for ndr ports"):
                    port_updated = False
                    for port in Configurations.ndr_ports[engine.ip]:
                        ndr_port = Port(port, "", "")
                        ndr_port_show = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                            ndr_port.interface.link.show(dut_engine=engine)).get_returned_value()

                        if ndr_port_show[LinkDetectionConsts.CONNECTION_MODE] != LinkDetectionConsts.CONNECTION_MODE_NDR:
                            ndr_port.interface.link.set(op_param_name=LinkDetectionConsts.CONNECTION_MODE,
                                                        op_param_value=LinkDetectionConsts.CONNECTION_MODE_NDR,
                                                        apply=False, dut_engine=engine,
                                                        ask_for_confirmation=True).verify_result()
                            port_updated = True

                    if apply and port_updated:
                        with allure.step("Apply configuration"):
                            general_cli = TestToolkit.GeneralApi[TestToolkit.tested_api]
                            output = general_cli.apply_config(engine=engine, option='--assume-yes')
                            assert "applied" in str(output).lower(), "Failed to apply config"

                        if wait_till_port_up:
                            ndr_port.interface.wait_for_port_state(state=NvosConsts.LINK_STATE_UP,
                                                                   logical_state=NvosConsts.LINK_LOG_STATE_ACTIVE)

        except BaseException as ex:
            if throw_exception:
                raise
            else:
                ExceptionTool.log_exception(ex)


class RegressionLinksConsts:
    SYSTEM_LINKS_PATH = "/auto/sw_system_project/NVOS_INFRA/verification_files/connectivity_files/"
    PORTS_ROOT = "ports"
    KEY_LOOPBACK = "loopback"
    KEY_CONNECTED_TO = "connected_to"
    KEY_NEIGHBOR_DESCRIPTION = "neighbor_description"
    KEY_LOGICAL_STATE = "state"
    KEY_PHYSICAL_STATE = "physical_state"
    TRANSCEIVER_TYPE = "transceiver_type"
    PORTS_LIST = "ports_list"
    CONNECTED_TO = "connected_to"
    CONNECTED_TO_SYSTEM_TYPE = "type"
    CONNECTED_TO_SYSTEM_NAME = "system"
    CONNECTED_TO_PORTS = "ports_list"
    TYPE_OPTICAL = "optical"
    TYPE_COPPER = "copper"
    TYPE_ACTIVE = "active"
    SYSTEM_TYPE_SERVER = "server"


class RegressionLinks:
    @staticmethod
    def _get_setup_links(setup_name):
        links_path = RegressionLinksConsts.SYSTEM_LINKS_PATH + setup_name + ".json"
        with allure.step(f'Read {setup_name} connectivity info from json {links_path}'):
            with open(links_path, 'r') as file:
                connectivity = json.load(file)
            return connectivity.get(RegressionLinksConsts.PORTS_ROOT, {})

    @staticmethod
    def get_filtered_ports_list(setup_name, is_loopback=False, connected_to=""):
        setup_links = RegressionLinks._get_setup_links(setup_name)
        filtered_ports = {}
        for port, port_data in setup_links.items():
            if port_data[RegressionLinksConsts.KEY_LOOPBACK] == is_loopback:
                filtered_ports[port] = port_data[RegressionLinksConsts.CONNECTED_TO]
        return filtered_ports

    @staticmethod
    def get_filtered_transceivers(setup_name, transceiver_type="", is_loopback=None, connected_to="",
                                  logical_states: List[str] = None, physical_states: List[str] = None) -> List[str]:
        """
        Get filtered transceivers (aggregated ports) based on the given parameters, using the new connectivity json.

        :param setup_name: The setup name to filter connections by
        :param transceiver_type: Ignored for the new connectivity format (kept for backward compatibility)
        :param is_loopback: Filter by loopback status (optional)
        :param connected_to: Filter by neighbor device/setup name (matches neighbor_description)
        :param logical_states: Optional list of allowed logical states (matches 'state')
        :param physical_states: Optional list of allowed physical states (matches 'physical_state')
        :return: A list of filtered transceivers
        """
        with allure.step(f'Get filtered transceivers for {setup_name}'):
            connections = RegressionLinks._get_setup_links(setup_name)
            filtered = []
            logical_set = set(logical_states) if logical_states else None
            physical_set = set(physical_states) if physical_states else None

            for transceiver, transceiver_data in connections.items():
                # loopback filter
                if is_loopback is not None and transceiver_data.get(RegressionLinksConsts.KEY_LOOPBACK) != is_loopback:
                    continue
                # connected_to (neighbor device/setup name)
                if connected_to:
                    neighbor_device = transceiver_data.get(RegressionLinksConsts.KEY_NEIGHBOR_DESCRIPTION, "")
                    if connected_to != neighbor_device:
                        continue
                # logical state filter
                if logical_set is not None:
                    if transceiver_data.get(RegressionLinksConsts.KEY_LOGICAL_STATE) not in logical_set:
                        continue
                # physical state filter
                if physical_set is not None:
                    if transceiver_data.get(RegressionLinksConsts.KEY_PHYSICAL_STATE) not in physical_set:
                        continue

                with allure.step(f"add {transceiver_data} to the filtered list"):
                    filtered.append(transceiver)

            allure.attach('filtered transceivers', filtered)
            return filtered

    @staticmethod
    def get_filtered_transceivers_and_ports(setup_name, transceiver_type="", is_loopback=None, connected_to="",
                                            logical_states: List[str] = None, physical_states: List[str] = None
                                            ) -> Dict[str, List[str]]:
        """
        Get filtered transceivers and the connected port label based on the given parameters.

        :param setup_name: The setup name to filter connections by
        :param transceiver_type: Ignored for the new connectivity format (kept for backward compatibility)
        :param is_loopback: Filter by loopback status (optional)
        :param connected_to: Filter by neighbor device/setup name (matches neighbor_description)
        :param logical_states: Optional list of allowed logical states (matches 'state')
        :param physical_states: Optional list of allowed physical states (matches 'physical_state')
        :return: {transceiver_name: [connected_port_label], ...}
        """
        with allure.step(f'Get filtered transceivers and ports for {setup_name}'):
            filtered_with_ports = {}
            connections = RegressionLinks._get_setup_links(setup_name)
            filtered_transceivers = RegressionLinks.get_filtered_transceivers(
                setup_name, transceiver_type, is_loopback, connected_to, logical_states, physical_states
            )
            for transceiver in filtered_transceivers:
                data = connections.get(transceiver, {})
                connected_label = data.get(RegressionLinksConsts.KEY_CONNECTED_TO)
                if connected_label:
                    filtered_with_ports[transceiver] = [connected_label]

            return filtered_with_ports

    @staticmethod
    def get_transceiver_data_and_port_index(setup_name: str, transceiver_name: str, port_name='') -> Tuple[Dict, int]:
        """
        Returns the dict describing the transceiver from the setup's connectivity json file.
        The connectivity format holds a single connected port label; port index is not applicable and will be None.
        """
        data = RegressionLinks._get_setup_links(setup_name)[transceiver_name]
        return data, None

    @staticmethod
    def get_loopback_end(setup_name: str, transceiver_name: str, port_name: str) -> str:
        """ Given a port in loopback connection, returns the name of the port at the other end of the cable. """
        transceiver, _ = RegressionLinks.get_transceiver_data_and_port_index(setup_name, transceiver_name, port_name)
        if not transceiver.get(RegressionLinksConsts.KEY_LOOPBACK):
            raise ValueError(f"{transceiver_name} is not a loopback connection: {transceiver}")
        result = transceiver.get(RegressionLinksConsts.KEY_CONNECTED_TO, "")
        logger.info(f"Port {port_name} is connected to {result}")
        return result

    @staticmethod
    def get_connected_host_and_port(setup_name: str, transceiver_name: str, port_name: str) -> Tuple[str, str]:
        """
        Returns the name of the neighbor device connected to the given port,
        and the name of the connected port label on the neighbor (as reported).
        """
        transceiver, _ = RegressionLinks.get_transceiver_data_and_port_index(setup_name, transceiver_name, port_name)
        host = transceiver.get(RegressionLinksConsts.KEY_NEIGHBOR_DESCRIPTION, "")
        host_port = transceiver.get(RegressionLinksConsts.KEY_CONNECTED_TO, "")
        logger.info(f"Port {port_name} is connected to {host} port {host_port}")
        return host, host_port
