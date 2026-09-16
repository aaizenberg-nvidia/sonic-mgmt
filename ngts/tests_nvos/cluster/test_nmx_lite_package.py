import json
import random
import re
import shlex
import string
import time

import pytest
import logging
from retry import retry
from retry.api import retry_call
from infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from ngts.nvos_constants.constants_nvos import (
    ApiType,
    HealthConsts,
    LinkHealthConsts,
    LogComponentsConsts,
    PeerPortConsts,
)
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.RegressionConfigurations import Configurations
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.nmx.PeerPort import PeerPort
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, ClusterSimulation
from ngts.tests_nvos.interfaces.nvl_port.helpers import toggle_port_state
from ngts.tests_nvos.interfaces.nvl_port.link_health_helpers import (
    _parse_link_health_state,
    _read_link_health_state,
    _read_peer_port_link_health_state,
    find_active_peer_switch_pair_for_attention,
    is_link_health_feature_available,
    is_link_health_supported,
    skip_if_peer_port_link_health_unavailable,
    trigger_link_health_attention_first,
    verify_healthy_link_health_block,
    verify_link_health_after_link_down,
    verify_link_health_state_valid,
    verify_peer_link_health_independent_of_switch_attention,
    verify_peer_port_link_health_show,
)
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.fixture(scope="module")
def start_cluster_and_sdn_fm_config(engines, setup_name):
    cluster = Cluster()
    topology_value = "vr_nvl8r1_c2g4_rtf_topology"
    try:
        with allure.step("Enable cluster"):
            ClusterTools.start_cluster(cluster, setup_name, engine=engines.dut)

        with allure.step("Config fm config"):
            ClusterSimulation.config_fm_config(engines.dut, topology_value=topology_value)

        with allure.step("Wait for nmx-controller to be in ok status"):
            ClusterTools.wait_until_app_expected_status(cluster, ClusterConsts.NMX_CONTROLLER, "ok", engine=engines.dut)

        yield

    finally:
        with allure.step("Reset sdn fm config"):
            with allure.step("Reset sdn factory default"):
                ClusterTools.reset_sdn_factory_default_and_wait_for_restart(Sdn(), cluster, engine=engines.dut)

            with allure.step("Disable cluster"):
                ClusterTools.stop_cluster(cluster, engine=engines.dut)


@pytest.mark.gpu_tel
@pytest.mark.skip_clear_config
@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_peer_port_show(engines, devices, test_api, start_cluster_and_sdn_fm_config):

    TestToolkit.tested_api = test_api
    peer_port = PeerPort(parent_obj=None)

    with allure.step(f"Validate 'nv show peer-port' command"):
        peer_port_list_output = OutputParsingTool.parse_json_str_to_dictionary(peer_port.show()).verify_result()
        assert peer_port_list_output, "peer-port list is empty"

    verify_link_health = is_link_health_supported(devices) and is_link_health_feature_available(engines.dut)

    with allure.step(f"Validate 'nv show peer-port --view=detail' for all Peer Ports"):
        detail_output = OutputParsingTool.parse_json_str_to_dictionary(
            peer_port.show(op_param='--view=detail')).verify_result()
        helper_validate_peer_port_detail_output(detail_output, peer_port_list_output,
                                                verify_link_health=verify_link_health)

    if test_api == ApiType.OPENAPI:
        # Querying each port separately hits the ACL rule hit_count limit and is blocked as a DoS,
        # so drill into a single port only - the detail view above already covered all of them.
        peer_ports = [peer_port.get_random_peer_port()]
    else:
        with allure.step(f"Get list of peer ports"):
            peer_ports = peer_port.get_list_of_ports()

    if verify_link_health:
        skip_if_peer_port_link_health_unavailable(engines.dut)

    with allure.step(f"Validate show commands for all Peer Ports"):
        logger.info(f"List of Peer Ports: {[port.peer_port_name for port in peer_ports]}")
        for port in peer_ports:
            with allure.step(f"Validate show commands for Peer Port {port.peer_port_name}"):
                helper_validate_peer_port_show(port)
                if verify_link_health:
                    verify_peer_port_link_health_show(port)


@pytest.mark.gpu_tel
@pytest.mark.skip_clear_config
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_peer_port_hostname_change(engines, test_api, setup_name, start_cluster_and_sdn_fm_config):
    TestToolkit.tested_api = test_api

    compute_nodes = Configurations.compute_nodes_per_system.get(setup_name)
    assert compute_nodes, f"setup '{setup_name}' has no compute_nodes_per_system"

    hostname_changes = []
    test_hostname_random_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    test_hostname_random_id = re.sub(r'gpu|nvidia', '', test_hostname_random_id, flags=re.IGNORECASE)  # Because NMX-T trims these markers
    if not test_hostname_random_id:
        test_hostname_random_id = str(random.randint(100000, 999999))

    try:
        with allure.step("Change hostnames on all compute node GPUs"):
            for compute_node in compute_nodes:
                for gpu_id in helper_list_nvidia_gpu_ids(compute_node):
                    test_hostname = f"NVOS-TEST-{test_hostname_random_id}-{gpu_id}"
                    hostname_changes.append((compute_node, gpu_id, test_hostname))

                    with allure.step(f"Set hostname to {test_hostname} on GPU {gpu_id} ({compute_node['ip_address']}) via nvidia-smi"):
                        helper_nvidia_smi_hostname(compute_node, gpu_id, 'set', test_hostname)

                    with allure.step(f"Verify nvidia-smi --get-hostname returns {test_hostname} for GPU {gpu_id} on {compute_node['ip_address']}"):
                        get_hostname_output = helper_nvidia_smi_hostname(compute_node, gpu_id, 'get')
                        assert test_hostname in get_hostname_output, (
                            f"Expected hostname {test_hostname} in nvidia-smi output, got: {get_hostname_output}"
                        )

        test_hostnames = {test_hostname for _, _, test_hostname in hostname_changes}
        with allure.step(f"Wait for all peer ports to show hostnames {test_hostnames}"):
            helper_wait_for_all_peer_ports_with_hostname(test_hostnames)

    finally:
        with allure.step(f"Restore hostnames to {PeerPortConsts.EXPECTED_GPU_HOSTNAME} on all compute node GPUs via nvidia-smi"):
            for compute_node, gpu_id, _test_hostname in hostname_changes:
                with allure.step(f"Restore hostname to {PeerPortConsts.EXPECTED_GPU_HOSTNAME} on GPU {gpu_id} ({compute_node['ip_address']})"):
                    helper_nvidia_smi_hostname(
                        compute_node, gpu_id, 'set', PeerPortConsts.EXPECTED_GPU_HOSTNAME
                    )

        with allure.step(f"Wait for all peer ports to restore hostname to {PeerPortConsts.EXPECTED_GPU_HOSTNAME}"):
            helper_wait_for_all_peer_ports_with_hostname(PeerPortConsts.EXPECTED_GPU_HOSTNAME)


@pytest.mark.gpu_tel
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_peer_port_fae_show(engines, test_api):

    TestToolkit.tested_api = test_api
    fae = Fae()
    with allure.step(f"Validate 'nv show fae peer-port' command"):
        OutputParsingTool.parse_json_str_to_dictionary(fae.system.peer_port.show()).verify_result()


@pytest.mark.gpu_tel
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_peer_port_state_fae_set(engines, test_api):

    TestToolkit.tested_api = test_api
    fae = Fae()
    try:
        helper_fae_peer_port_disable_enable(fae)

    finally:
        with allure.step(f"Validate unset peer-port state command"):
            fae.system.peer_port.unset(op_param=PeerPortConsts.STATE, apply=True).verify_result()

        with allure.step(f"Validate peer-port state is set to default (enabled) in show command"):
            show_output = OutputParsingTool.parse_json_str_to_dictionary(fae.system.peer_port.show()).verify_result()
            ValidationTool.verify_field_value_in_output(show_output, PeerPortConsts.STATE, PeerPortConsts.STATE_ENABLED).\
                verify_result()


@pytest.mark.gpu_tel
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_peer_port_state_fae_set_unset_stress(engines, test_api):

    TestToolkit.tested_api = test_api
    fae = Fae()
    system = System()
    try:
        for i in range(20):
            helper_fae_peer_port_disable_enable(fae)
            with allure.step(f"Check health status"):
                health_op = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).verify_result()
                ValidationTool.verify_field_value_in_output(health_op, HealthConsts.STATUS, HealthConsts.OK).\
                    verify_result()

    finally:
        with allure.step(f"Validate unset peer-port state command"):
            fae.system.peer_port.unset(op_param=PeerPortConsts.STATE, apply=True).verify_result()

        with allure.step(f"Validate peer-port state is set to default (enabled) in show command"):
            show_output = OutputParsingTool.parse_json_str_to_dictionary(fae.system.peer_port.show()).verify_result()
            ValidationTool.verify_field_value_in_output(show_output, PeerPortConsts.STATE, PeerPortConsts.STATE_ENABLED).\
                verify_result()


@pytest.mark.gpu_tel
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_nmx_t_lite_start_stop(engines, test_api):

    TestToolkit.tested_api = test_api
    fae = Fae()
    system = System()
    nmx_t_lite = "nmx-t-lite"
    try:
        with allure.step(f"Check health status"):
            with allure.step(f"Check health status"):
                retry_call(helper_validate_health_status, [system], exceptions=AssertionError, tries=1, delay=1)

        with allure.step(f"Stop nmx-t-lite"):
            engines.dut.run_cmd(f"sudo systemctl stop {nmx_t_lite}.service")
            helper_check_service_status(engines, nmx_t_lite, "inactive")

        with allure.step(f"Check health status"):
            retry_call(helper_validate_health_status, [system], exceptions=AssertionError, tries=1, delay=1)

    finally:
        with allure.step(f"Start nmx-t-lite"):
            engines.dut.run_cmd(f"sudo systemctl start {nmx_t_lite}.service")
            helper_check_service_status(engines, nmx_t_lite, "active")

        with allure.step(f"Check health status"):
            retry_call(helper_validate_health_status, [system], exceptions=AssertionError, tries=1, delay=1)


@pytest.mark.gpu_tel
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_peer_port_state_services(engines, test_api):

    TestToolkit.tested_api = test_api
    fae = Fae()
    gpu_t_service = PeerPortConsts.GPU_TELEMETRY_SERVICE
    nmx_t_lite_service = PeerPortConsts.NMX_T_LITE_SERVICE
    with allure.step(f"Validate peer-port state is enabled by default in show command"):
        show_output = OutputParsingTool.parse_json_str_to_dictionary(fae.system.peer_port.show()).verify_result()
        ValidationTool.verify_field_value_in_output(show_output, PeerPortConsts.STATE, PeerPortConsts.STATE_ENABLED).\
            verify_result()

    with allure.step(f"Validate GPU Telemetry service is running"):
        helper_check_service_status(engines, gpu_t_service, "active")

    with allure.step(f"Validate NMX Telemetry Lite service is running"):
        helper_check_service_status(engines, nmx_t_lite_service, "active")

    with allure.step(f"Validate NMX Telemetry Lite docker is running"):
        helper_check_docker_status(engines, nmx_t_lite_service, True)

    with allure.step(f"Validate set disable peer-port state command"):
        fae.system.peer_port.set(op_param_name=PeerPortConsts.STATE, op_param_value=PeerPortConsts.STATE_DISABLED,
                                 apply=True).verify_result()

    with allure.step(f"Validate peer-port state is disabled in show command"):
        show_output = OutputParsingTool.parse_json_str_to_dictionary(fae.system.peer_port.show()).verify_result()
        ValidationTool.verify_field_value_in_output(show_output, PeerPortConsts.STATE, PeerPortConsts.STATE_DISABLED).\
            verify_result()

    with allure.step(f"Validate GPU Telemetry service is running"):
        helper_check_service_status(engines, gpu_t_service, "active")

    with allure.step(f"Validate NMX Telemetry Lite service is not running"):
        helper_check_service_status(engines, nmx_t_lite_service, "inactive")

    with allure.step(f"Validate NMX Telemetry Lite docker is not running"):
        helper_check_docker_status(engines, nmx_t_lite_service, False)

    with allure.step(f"Validate unset peer-port state command"):
        fae.system.peer_port.unset(op_param=PeerPortConsts.STATE, apply=True).verify_result()

    with allure.step(f"Validate peer-port state is set to default (enabled) in show command"):
        show_output = OutputParsingTool.parse_json_str_to_dictionary(fae.system.peer_port.show()).verify_result()
        ValidationTool.verify_field_value_in_output(show_output, PeerPortConsts.STATE, PeerPortConsts.STATE_ENABLED).\
            verify_result()

    with allure.step(f"Validate GPU Telemetry service is running"):
        helper_check_service_status(engines, gpu_t_service, "active")

    with allure.step(f"Validate NMX Telemetry Lite service is running"):
        helper_check_service_status(engines, nmx_t_lite_service, "active")

    with allure.step(f"Validate NMX Telemetry Lite docker is running"):
        helper_check_docker_status(engines, nmx_t_lite_service, True)


@pytest.mark.gpu_tel
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_gpu_telemetry_log_level_set(engines, test_api):

    TestToolkit.tested_api = test_api
    system = System()
    peer_telemetry = LogComponentsConsts.PEER_TELEMETRY

    for log_level in LogComponentsConsts.LOG_LEVEL_LIST:
        with allure.step("Validate set peer telemetry log level command"):
            system.log.component.component_id[peer_telemetry].set(op_param_name=LogComponentsConsts.LEVEL,
                                                                  op_param_value=log_level, apply=True).verify_result()

        with allure.step(f"Validate peer telemetry log level is set to {log_level} in show command"):
            show_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.log.component.component_id[peer_telemetry].show()).verify_result()
            ValidationTool.verify_field_value_in_output(show_output, LogComponentsConsts.LEVEL, log_level).\
                verify_result()

    with allure.step("Validate unset peer telemetry log level command"):
        system.log.component.component_id[peer_telemetry].unset(op_param=LogComponentsConsts.LEVEL, apply=True).\
            verify_result()

    with allure.step(f"Validate peer telemetry log level is set to default {LogComponentsConsts.LOG_LEVEL_DEFAULT}"):
        show_output = OutputParsingTool.parse_json_str_to_dictionary(system.log.component.component_id[peer_telemetry].
                                                                     show()).verify_result()
        ValidationTool.verify_field_value_in_output(show_output, LogComponentsConsts.LEVEL,
                                                    LogComponentsConsts.LOG_LEVEL_DEFAULT).verify_result()


@pytest.mark.gpu_tel
@pytest.mark.skip_clear_config
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_peer_port_disconnect(engines, test_api, start_cluster_and_sdn_fm_config):

    TestToolkit.tested_api = test_api
    peer_port = PeerPort(parent_obj=None)

    with allure.step(f"Get random peer port"):
        random_peer_port = peer_port.get_random_peer_port()
        assert random_peer_port is not None, "Port list is empty"

    with allure.step(f"Get the associated switch port for the peer port {random_peer_port.peer_port_name}"):
        peer_port_show = OutputParsingTool.parse_json_str_to_dictionary(random_peer_port.peer_port.show()).verify_result()
        switch_port_name = peer_port_show[PeerPortConsts.ASSOCIATED_SWITCH_PORT]
        switch_port = Port(switch_port_name)

    with allure.step(f"Link down the associated switch port {switch_port_name}"):
        port_state = NvosConsts.LINK_STATE_DOWN
        switch_port.interface.link.state.set(op_param_name=port_state, apply=True,
                                             ask_for_confirmation=True).verify_result()

    with allure.step("Wait till port {} is {}".format(switch_port, port_state)):
        switch_port.interface.wait_for_port_state(port_state)

    with allure.step(f"Check the peer port shall be removed from list of peer ports"):
        helper_find_port_in_peer_ports(peer_port, random_peer_port, False)

    with allure.step(f"Link back Up the associated switch port {switch_port_name}"):
        port_state = NvosConsts.LINK_STATE_UP
        switch_port.interface.link.state.set(op_param_name=port_state, apply=True,
                                             ask_for_confirmation=True).verify_result()

    with allure.step("Wait till port {} is {}".format(switch_port, port_state)):
        switch_port.interface.wait_for_port_state(port_state)

    with allure.step(f"Check the peer port shall be available from list of peer ports"):
        helper_find_port_in_peer_ports(peer_port, random_peer_port, True)

    with allure.step(f"Verify hostname after peer port {random_peer_port.peer_port_name} is restored"):
        peer_port_show = OutputParsingTool.parse_json_str_to_dictionary(
            random_peer_port.peer_port.show()).verify_result()
        helper_verify_peer_port_hostname(peer_port_show, random_peer_port.peer_port_name)


@pytest.mark.gpu_tel
def test_link_health_switch_state_vs_peer_status(engines, devices, test_name):
    """Correlate switch-side down/attention behavior with the associated peer port.

    Asserted semantics:
        - switch admin down: the peer leaves the peer-port list, and comes back
          when the switch port is brought up again.
        - switch attention: the link stays up, so the peer stays in the list and
          keeps its own state (``healthy`` or ``n/a``) - a switch-side latch must
          never mirror onto the GPU peer path (PP-07, separate telemetry feeds).
    """
    dut_engine = engines.dut
    if not (is_link_health_supported(devices) and is_link_health_feature_available(dut_engine)):
        pytest.skip('Link-health feature not available on this platform')

    peer_port = PeerPort(parent_obj=None)
    peer_intf, switch_port = find_active_peer_switch_pair_for_attention(peer_port)
    if peer_intf is None:
        pytest.skip(
            'No Active + healthy peer/switch pair available for switch<->peer correlation'
        )
    peer_name = peer_intf.peer_port_name

    switch_down = False
    switch_attention = False
    try:
        with allure.step(f'Baseline: switch {switch_port.name} and peer {peer_name}'):
            sw_state, _ = _read_link_health_state(switch_port)
            peer_state, _ = _read_peer_port_link_health_state(peer_intf)
            verify_link_health_state_valid(peer_state, peer_name)
            helper_find_port_in_peer_ports(peer_port, peer_intf, available=True)
            logger.info(
                f'CORRELATION baseline: switch {switch_port.name}={sw_state!r}, '
                f'peer {peer_name}={peer_state!r}'
            )

        with allure.step(
            f'Switch {switch_port.name} admin down -> peer {peer_name} removed from peer-port list'
        ):
            toggle_port_state(switch_port, NvosConsts.LINK_STATE_DOWN, test_name, devices)
            switch_down = True
            time.sleep(15)
            verify_link_health_after_link_down(switch_port, LinkHealthConsts.STATE_HEALTHY)
            helper_find_port_in_peer_ports(peer_port, peer_intf, available=False)
            logger.info(
                f'CORRELATION switch-down: switch {switch_port.name} is down -> '
                f'peer {peer_name} removed from peer-port list'
            )

        with allure.step(f'Restore switch {switch_port.name} up -> peer {peer_name} re-appears'):
            toggle_port_state(switch_port, NvosConsts.LINK_STATE_UP, test_name, devices)
            switch_down = False
            time.sleep(15)
            helper_find_port_in_peer_ports(peer_port, peer_intf, available=True)

        with allure.step(
            f'Switch {switch_port.name} attention -> observe peer {peer_name}'
        ):
            worked = trigger_link_health_attention_first(switch_port, dut_engine)
            if worked is None:
                pytest.skip(
                    f'Could not induce switch-side attention on {switch_port.name}; '
                    'cannot evaluate switch-attention vs peer correlation'
                )
            switch_attention = True
            peer_state, peer_out = verify_peer_link_health_independent_of_switch_attention(
                peer_intf, switch_port
            )
            helper_find_port_in_peer_ports(peer_port, peer_intf, available=True)
            logger.info(
                f'CORRELATION switch-attention: switch {switch_port.name} is attention, '
                f'peer {peer_name} link-health={peer_state!r} (output={peer_out!r})'
            )
    finally:
        with allure.step(f'Recover switch {switch_port.name} via down/up'):
            if switch_attention or not switch_down:
                toggle_port_state(switch_port, NvosConsts.LINK_STATE_DOWN, test_name, devices)
                time.sleep(10)
            toggle_port_state(switch_port, NvosConsts.LINK_STATE_UP, test_name, devices)


def helper_fae_peer_port_disable_enable(fae):
    with allure.step(f"Validate set enable peer-port state command"):
        fae.system.peer_port.set(op_param_name=PeerPortConsts.STATE, op_param_value=PeerPortConsts.STATE_ENABLED,
                                 apply=True).verify_result()

    with allure.step(f"Validate peer-port state is enabled in show command"):
        show_output = OutputParsingTool.parse_json_str_to_dictionary(fae.system.peer_port.show()).verify_result()
        ValidationTool.verify_field_value_in_output(show_output, PeerPortConsts.STATE, PeerPortConsts.STATE_ENABLED). \
            verify_result()

    with allure.step(f"Validate set disable peer-port state command"):
        fae.system.peer_port.set(op_param_name=PeerPortConsts.STATE, op_param_value=PeerPortConsts.STATE_DISABLED,
                                 apply=True).verify_result()

    with allure.step(f"Validate peer-port state is disabled in show command"):
        show_output = OutputParsingTool.parse_json_str_to_dictionary(fae.system.peer_port.show()).verify_result()
        ValidationTool.verify_field_value_in_output(show_output, PeerPortConsts.STATE, PeerPortConsts.STATE_DISABLED). \
            verify_result()


@retry(AssertionError, tries=36, delay=5)
def helper_find_port_in_peer_ports(peer_port, peer_port_interface, available=True):
    port_found = True if peer_port_interface.peer_port_name in peer_port.peer_port_names_get() else False
    assert port_found == available, 'Port {name} find status in list of peer ports is {found} instead of {available}'.\
        format(name=peer_port_interface.peer_port_name, found=port_found, available=available)


def helper_validate_peer_port_show(peer_port_interface):
    with allure.step(f"Validate show_peer_port API for Port {peer_port_interface.peer_port_name}"):
        OutputParsingTool.parse_json_str_to_dictionary(peer_port_interface.show_peer_port()).verify_result()

    with allure.step(f"Validate 'nv show peer-port <port>' for Port {peer_port_interface.peer_port_name}"):
        peer_port_detail_output = OutputParsingTool.parse_json_str_to_dictionary(
            peer_port_interface.peer_port.show()).verify_result()
        helper_verify_peer_port_hostname(peer_port_detail_output, peer_port_interface.peer_port_name)

    with allure.step(f"Validate 'nv show peer-port <port> counters' for Port {peer_port_interface.peer_port_name}"):
        OutputParsingTool.parse_json_str_to_dictionary(peer_port_interface.peer_port.counters.show()).verify_result()

    with allure.step(f"Validate 'nv show peer-port <port> counters link' for Port {peer_port_interface.peer_port_name}"):
        OutputParsingTool.parse_json_str_to_dictionary(peer_port_interface.peer_port.counters.link.show()).verify_result()

    with allure.step(f"Validate 'nv show peer-port <port> counters nvl' for Port {peer_port_interface.peer_port_name}"):
        OutputParsingTool.parse_json_str_to_dictionary(peer_port_interface.peer_port.counters.nvl.show()).verify_result()

    with allure.step(f"Validate 'nv show peer-port <port> link' for Port {peer_port_interface.peer_port_name}"):
        OutputParsingTool.parse_json_str_to_dictionary(peer_port_interface.peer_port.link.show()).verify_result()

    with allure.step(f"Validate 'nv show peer-port <port> link phy' for Port {peer_port_interface.peer_port_name}"):
        OutputParsingTool.parse_json_str_to_dictionary(peer_port_interface.peer_port.link.phy.show()).verify_result()

    with allure.step(f"Validate 'nv show peer-port <port> link phy detail' for {peer_port_interface.peer_port_name}"):
        OutputParsingTool.parse_json_str_to_dictionary(peer_port_interface.peer_port.link.phy.detail.show()).\
            verify_result()

    with allure.step(f"Validate 'nv show peer-port <port> link phy health' for {peer_port_interface.peer_port_name}"):
        OutputParsingTool.parse_json_str_to_dictionary(peer_port_interface.peer_port.link.phy.health.show()).\
            verify_result()

    with allure.step(f"Validate 'nv show peer-port <port> link phy health histogram' for Port"
                     f" {peer_port_interface.peer_port_name}"):
        OutputParsingTool.parse_json_str_to_dictionary(peer_port_interface.peer_port.link.phy.health.histogram.show()).\
            verify_result()


def helper_validate_peer_port_detail_output(detail_output, expected_ports, verify_link_health=False):
    """
    Same coverage as helper_validate_peer_port_show, but the detail view already holds the data of
    every drill-in command, so each port is verified against the dict instead of being queried again.
    """
    assert set(detail_output) == set(expected_ports), \
        f"Detail view returned ports {sorted(detail_output)}, expected {sorted(expected_ports)}"

    for port_name, port_data in detail_output.items():
        with allure.step(f"Validate detail output for Peer Port {port_name}"):
            helper_verify_peer_port_hostname(port_data, port_name)

            assert port_data.get(PeerPortConsts.ASSOCIATED_SWITCH_PORT), \
                f"Peer Port {port_name}: '{PeerPortConsts.ASSOCIATED_SWITCH_PORT}' is missing or empty"

            for block_path in PeerPortConsts.DETAIL_VIEW_BLOCKS:
                block_name = ' '.join(block_path)
                with allure.step(f"Validate '{block_name}' block for Peer Port {port_name}"):
                    block = port_data
                    for key in block_path:
                        assert isinstance(block, dict) and key in block, \
                            f"Peer Port {port_name}: block '{block_name}' is missing from detail output"
                        block = block[key]
                    assert block, \
                        f"Peer Port {port_name}: block '{block_name}' is empty in detail output"

            if verify_link_health:
                with allure.step(f"Verify peer-port link health for {port_name}"):
                    health_output = port_data[PeerPortConsts.LINK][PeerPortConsts.HEALTH]
                    state = _parse_link_health_state(health_output)
                    verify_link_health_state_valid(state, port_name)
                    if state == LinkHealthConsts.STATE_HEALTHY:
                        verify_healthy_link_health_block(health_output, port_name)


def helper_verify_peer_port_hostname(port_output, port_name="", expected_hostname=None):
    expected_hostname = expected_hostname or PeerPortConsts.EXPECTED_GPU_HOSTNAME
    step_name = f"Verify hostname for {port_name}" if port_name else "Verify hostname"
    with allure.step(step_name):
        ValidationTool.verify_field_value_in_output(
            port_output,
            PeerPortConsts.HOSTNAME,
            expected_hostname,
        ).verify_result()


def helper_exec_on_compute_node(compute_node, cmd, use_sudo=False):
    ip_address = compute_node['ip_address']
    username = compute_node['username']
    password = compute_node['password']
    engine = LinuxSshEngine(ip_address, username, password)

    if use_sudo:
        remote_cmd = (
            f"printf '%s\\n' '{password}' | sudo -S -p '' sh -c {shlex.quote(cmd)}"
        )
    else:
        remote_cmd = cmd

    full_cmd = f"{remote_cmd} ; echo COMPUTE__CMD__\"DONE\""
    output = engine.send_cmd_with_retry(
        full_cmd, timeout=60, retries=2,
        auto_find_prompt=False,
        expected_string='COMPUTE__CMD__DONE')
    return "\n".join(
        line for line in (output or "").splitlines()
        if "COMPUTE__CMD__DONE" not in line).strip()


def helper_list_nvidia_gpu_ids(compute_node):
    output = helper_exec_on_compute_node(compute_node, "nvidia-smi -L")
    gpu_ids = []
    for line in (output or "").splitlines():
        match = re.match(r'GPU (\d+):', line)
        if match:
            gpu_ids.append(int(match.group(1)))
    assert gpu_ids, f"No GPUs found on compute node {compute_node['ip_address']}: {output!r}"
    return gpu_ids


@retry(AssertionError, tries=12, delay=5)
def helper_wait_for_all_peer_ports_with_hostname(expected_hostnames):
    if isinstance(expected_hostnames, str):
        expected_hostnames = {expected_hostnames}

    peer_port = PeerPort(parent_obj=None)
    list_output = OutputParsingTool.parse_json_str_to_dictionary(peer_port.show()).verify_result()
    total_peer_ports = len(list_output)
    assert total_peer_ports, "peer-port list is empty"

    matching_peer_ports = [
        peer_port_name
        for peer_port_name, port_data in list_output.items()
        if port_data.get(PeerPortConsts.HOSTNAME) in expected_hostnames
    ]
    assert len(matching_peer_ports) == total_peer_ports, (
        f"Expected all {total_peer_ports} peer ports with hostname in {expected_hostnames}"
    )


def helper_nvidia_smi_hostname(compute_node, gpu_id, hostname_op, hostname=None):
    if hostname_op == 'set':
        assert hostname, "hostname is required for set operation"
        hostname_args = f"--set-hostname {hostname}"
    elif hostname_op == 'get':
        hostname_args = "--get-hostname"
    else:
        raise ValueError(f"Unsupported nvidia-smi hostname operation: {hostname_op}")

    nvidia_smi_cmd = f"nvidia-smi -i {gpu_id} {hostname_args}"
    use_sudo = hostname_op == 'set'
    output = helper_exec_on_compute_node(compute_node, nvidia_smi_cmd, use_sudo=use_sudo)
    logger.info(
        "nvidia-smi -i %s %s on %s output:\n%s",
        gpu_id, hostname_args, compute_node['ip_address'], output
    )
    return output


@retry(AssertionError, tries=3, delay=4)
def helper_check_service_status(engines, service, exp_status):
    status = engines.dut.run_cmd(f"sudo systemctl is-active {service}.service")
    assert exp_status == status, f"Status of service {service} is {status} instead of {exp_status}"


@retry(AssertionError, tries=3, delay=4)
def helper_check_docker_status(engines, service, running_status):
    output = engines.dut.run_cmd(f'docker ps --filter "name=^/{service}" --filter "status=running" -q')
    assert bool(output) == running_status, f"Running Status of docker {service} is not {running_status}"


def helper_validate_health_status(system, status=HealthConsts.OK):
    health_op = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).verify_result()
    ValidationTool.verify_field_value_in_output(health_op, HealthConsts.STATUS, status).verify_result()
