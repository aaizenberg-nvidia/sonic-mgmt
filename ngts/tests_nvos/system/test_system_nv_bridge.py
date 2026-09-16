import time
import pytest
import random
import logging
from typing import Dict
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests.nightly.dynamic_port_breakout.conftest import dut_engine
from ngts.nvos_constants.constants_nvos import ClusterApps
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_constants.constants_nvos import SystemConsts, OutputFormat, ChassisLocationConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_constants.constants_nvos import HealthConsts, ApiType
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.general.security.password_hardening.PwhConsts import PwhConsts
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from retry import retry
from datetime import datetime

logger = logging.getLogger(__name__)


@pytest.mark.nv_bridge
@pytest.mark.system
def test_system_nv_bridge_default_fields_values(engines, devices, nv_command, random_api):
    """
    Test flow:
        1. Check default output
        2. Enable cluster
        3. Set cluster node
        4. Check nv-bridge enable
        5. Check system logs
        6. Check acl
        7. Unset cluster
        8. Check output after unset
    """
    system = nv_command.system
    nv_bridge = nv_command.system.nv_bridge
    acl = nv_command.acl

    try:
        with allure.step("Check default values for cpu-debug-config output"):
            _verify_nv_bridge_output(nv_bridge, state=SystemConsts.NV_BRIDGE_ENABLED, dut_engine=engines.dut)

        with allure.step('Start cluster'):
            cluster = Cluster()

            with allure.step("Start cluster"):
                output = OutputParsingTool.parse_show_output_to_dict(
                    cluster.show(output_format=OutputFormat.json),
                    output_format=OutputFormat.json).get_returned_value()

                if output[SystemConsts.STATE] == SystemConsts.CLUSTER_STATE_DISABLED:
                    cluster.set(op_param_name=SystemConsts.STATE, op_param_value=SystemConsts.CLUSTER_STATE_ENABLED, apply=True)
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state=SystemConsts.CLUSTER_STATE_ENABLED,
                                                                     nmx_c_expected_state=SystemConsts.CLUSTER_APP_STATE_UP)

        with allure.step("Set cluster node"):
            cluster.node.primary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER, op_param_value=engines.dut.ip, apply=True)

        with allure.step("Verify nv bridge output"):
            _verify_nv_bridge_output(nv_bridge, state=SystemConsts.NV_BRIDGE_ENABLED, heart_beat=SystemConsts.NV_BRIDGE_HEALTH_OK, connections=engines.dut.ip, local_host=True, dut_engine=engines.dut)

            with allure.step("Check system logs"):
                TestToolkit.tested_api = ApiType.NVUE
                show_output = system.log.file.show_log(param=SystemConsts.NV_BRIDGE_GREP)
                TestToolkit.tested_api = random_api
                ValidationTool.verify_expected_output(show_output, SystemConsts.NV_BRIDGE).verify_result()

        with allure.step("Verify default nv-bridge acl"):
            ipv4_nv_bridge_acl = OutputParsingTool.parse_json_str_to_dictionary(acl.acl_id[SystemConsts.ACL_DEFAULT_WHITELIST].rule.rule_id[SystemConsts.NV_BRIDGE_IPV4_ACL_RULE].parse_show()).get_returned_value()
            assert SystemConsts.NV_BRIDGE_PORT not in ipv4_nv_bridge_acl, f'{SystemConsts.NV_BRIDGE_PORT} not in acl rule {ipv4_nv_bridge_acl}'
            ipv6_nv_bridge_acl = OutputParsingTool.parse_json_str_to_dictionary(
                acl.acl_id[SystemConsts.ACL_DEFAULT_WHITELIST_IPV6].rule.rule_id[SystemConsts.NV_BRIDGE_IPV6_ACL_RULE].parse_show()).get_returned_value()
            assert SystemConsts.NV_BRIDGE_PORT not in ipv6_nv_bridge_acl, f'{SystemConsts.NV_BRIDGE_PORT} not in acl rule {ipv6_nv_bridge_acl}'

    finally:
        with allure.step("Unset node"):
            cluster.unset(apply=True)

            with allure.step("Verify output after unset node"):
                _verify_nv_bridge_output(nv_bridge, state=SystemConsts.NV_BRIDGE_ENABLED, dut_engine=engines.dut)


@pytest.mark.nv_bridge
@pytest.mark.system
def test_system_nv_bridge_functional(engines, devices, nv_command, dut_engines, random_api, single_switch):
    """
    Test flow:
        1. Start Cluster
        2. Configure NV bridge
        3. Verify NV bridge output
        4. Send MAD and check no errors
        5. Unset Cluster
    """
    if single_switch:
        pytest.skip("This test needs at least 2 switches t run")

    dut, dut2 = random.sample(list(dut_engines.values()), k=2)
    system = nv_command.system
    nv_bridge = nv_command.system.nv_bridge

    try:
        with allure.step('Start cluster'):
            cluster = Cluster()

            with allure.step("Start cluster"):
                output = OutputParsingTool.parse_show_output_to_dict(
                    cluster.show(output_format=OutputFormat.json, dut_engine=dut),
                    output_format=OutputFormat.json).get_returned_value()

                if output[SystemConsts.STATE] == SystemConsts.CLUSTER_STATE_DISABLED:
                    cluster.set(op_param_name=SystemConsts.STATE, op_param_value=SystemConsts.CLUSTER_STATE_ENABLED,
                                apply=True, dut_engine=dut)
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state=SystemConsts.CLUSTER_STATE_ENABLED,
                                                                     nmx_c_expected_state=SystemConsts.CLUSTER_APP_STATE_UP, engine=dut)

        with allure.step("Check default values for nv-bridge output"):
            cluster.node.primary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER, op_param_value=dut.ip,
                                                  apply=True, dut_engine=dut)
            cluster.node.primary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER, op_param_value=dut2.ip,
                                                  apply=True, dut_engine=dut)

        with allure.step("Verify output after configure nv bridge"):
            dut_hostname = dut.run_cmd("hostname")
            _verify_nv_bridge_output(nv_bridge, state=SystemConsts.NV_BRIDGE_ENABLED, dut_engine=dut, connections=True,
                                     active=SystemConsts.NV_BRIDGE_CLIENT_ACTIVE, client_address=dut.ip,
                                     client_id=dut_hostname, server_address=dut.ip)
            _verify_nv_bridge_output(nv_bridge, state=SystemConsts.NV_BRIDGE_ENABLED, dut_engine=dut2, connections=True,
                                     active=SystemConsts.NV_BRIDGE_CLIENT_ACTIVE, client_address=dut.ip,
                                     client_id=dut_hostname, server_address=dut2.ip)

        with allure.step("Get tray index of second dut"):
            tray_index = OutputParsingTool.parse_show_output_to_dict(nv_command.platform.chassis_location.show(dut_engine=dut2)).get_returned_value()[ChassisLocationConsts.TRAY_ID]

        with allure.step("Send MAD and check it's successfully"):
            _send_mad_verify_no_errors(dut, tray_index)

    finally:
        with allure.step("Unset node"):
            cluster.unset(apply=True, dut_engine=dut)


@pytest.mark.nv_bridge
@pytest.mark.system
def test_system_nv_bridge_negative(engines, devices, nv_command, random_api):
    """
    Test flow:
        1. Start Cluster
        2. Configure NV bridge without ip
        3. Configure NV bridge with not nv bridge cluster ip
        4. Unset Cluster
    """

    system = nv_command.system
    nv_bridge = nv_command.system.nv_bridge

    try:
        with allure.step('Start cluster'):
            cluster = Cluster()

            with allure.step("Start cluster"):
                output = OutputParsingTool.parse_show_output_to_dict(
                    cluster.show(output_format=OutputFormat.json),
                    output_format=OutputFormat.json).get_returned_value()

                if output[SystemConsts.STATE] == SystemConsts.CLUSTER_STATE_DISABLED:
                    cluster.set(op_param_name=SystemConsts.STATE, op_param_value=SystemConsts.CLUSTER_STATE_ENABLED, apply=True)
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state=SystemConsts.CLUSTER_STATE_ENABLED,
                                                                     nmx_c_expected_state=SystemConsts.CLUSTER_APP_STATE_UP)

        with allure.step("Set cluster node without ip"):
            err_msg = PwhConsts.ERR_INCOMPLETE_SET_CMD
            if random_api == ApiType.OPENAPI:
                err_msg = "Error: Server address must be a valid IPv4 or IPv6 address"
            cluster.node.primary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER, op_param_value='', expected_str=err_msg)

        with allure.step("Set cluster negative ip"):
            cluster.node.primary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER, apply=True, op_param_value=SystemConsts.NV_BRIDGE_NODE_NEGATIVE_IP)

        with allure.step("Verify output after configure wrong nv bridge node"):
            _verify_nv_bridge_output(nv_bridge, state=SystemConsts.NV_BRIDGE_ENABLED, no_connection=True, dut_engine=engines.dut)

        with allure.step("Check system status is OK"):
            system.validate_health_status(HealthConsts.OK)

    finally:
        with allure.step("Unset node"):
            cluster.unset(apply=True)


@pytest.mark.nv_bridge
@pytest.mark.system
def test_system_nv_bridge_simulate_issue(engines, devices, nv_command, dut_engines, random_api, single_switch):
    """
    Test flow:
        1. Configure Cluster
        2. Configure NV-bridge
        3. Check NV-bridge configured
        4. Restart NV-bridge docker
        5. Check NV-bridge connection restored after restart
        6. Unset Cluster
    """
    if single_switch:
        pytest.skip("This test needs at least 2 switches t run")

    dut, dut2 = random.sample(list(dut_engines.values()), k=2)

    system = nv_command.system
    nv_bridge = nv_command.system.nv_bridge

    try:
        with allure.step('Start cluster'):
            cluster = Cluster()
            with allure.step("Start cluster"):
                output = OutputParsingTool.parse_show_output_to_dict(
                    cluster.show(output_format=OutputFormat.json, dut_engine=dut),
                    output_format=OutputFormat.json).get_returned_value()

                if output[SystemConsts.STATE] == SystemConsts.CLUSTER_STATE_DISABLED:
                    cluster.set(op_param_name=SystemConsts.STATE, op_param_value=SystemConsts.CLUSTER_STATE_ENABLED, apply=True, dut_engine=dut)
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state=SystemConsts.CLUSTER_STATE_ENABLED,
                                                                     nmx_c_expected_state=SystemConsts.CLUSTER_APP_STATE_UP, engine=dut)

        with allure.step("Check default values for nv-bridge output"):
            cluster.node.primary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER, op_param_value=dut.ip,
                                                  apply=True, dut_engine=dut)
            cluster.node.primary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER, op_param_value=dut2.ip,
                                                  apply=True, dut_engine=dut)

        with allure.step("Verify output after configure nv bridge"):
            dut_hostname = dut.run_cmd("hostname")
            _verify_nv_bridge_output(nv_bridge, state=SystemConsts.NV_BRIDGE_ENABLED, dut_engine=dut, connections=True,
                                     active=SystemConsts.NV_BRIDGE_CLIENT_ACTIVE, client_address=dut.ip,
                                     client_id=dut_hostname, server_address=dut.ip)
            _verify_nv_bridge_output(nv_bridge, state=SystemConsts.NV_BRIDGE_ENABLED, dut_engine=dut2, connections=True,
                                     active=SystemConsts.NV_BRIDGE_CLIENT_ACTIVE, client_address=dut.ip,
                                     client_id=dut_hostname, server_address=dut2.ip)

        with allure.step("Simulate nv-bridge docker issue"):
            start_time = datetime.now()
            _restart_nv_bridge_container(dut2)
            time.sleep(10)

        with allure.step("Check system status"):
            system.health.history.search_line_by_date(lines_to_search=["nv-bridge: Unreachable", "Summary: Not OK"],
                                                      since_date=start_time, expect_found=True,
                                                      file_output=dut2.run_cmd("sudo cat /var/log/health_history"))
            system.health.history.search_line_by_date(lines_to_search=["nv-bridge: Unreachable", "Summary: Not OK"],
                                                      since_date=start_time, expect_found=False,
                                                      file_output=dut.run_cmd("sudo cat /var/log/health_history"))

        with allure.step("Verify output after configure nv bridge"):
            dut_hostname = dut.run_cmd("hostname")
            _verify_nv_bridge_output(nv_bridge, state=SystemConsts.NV_BRIDGE_ENABLED, dut_engine=dut,
                                     connections=True, active=SystemConsts.NV_BRIDGE_CLIENT_ACTIVE,
                                     client_address=engines.dut.ip, client_id=dut_hostname, server_address=dut.ip)
            _verify_nv_bridge_output(nv_bridge, state=SystemConsts.NV_BRIDGE_ENABLED, dut_engine=dut2,
                                     connections=True, active=SystemConsts.NV_BRIDGE_CLIENT_ACTIVE,
                                     client_address=dut.ip, client_id=dut_hostname,
                                     server_address=dut2.ip)
    finally:
        with allure.step("Unset node"):
            cluster.unset(apply=True, dut_engine=dut)


@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
@pytest.mark.nv_bridge
@pytest.mark.system
def test_system_nv_bridge_state_file(engines, nv_command, test_api):
    """
    Test flow:
        1. Enable cluster if not enabled
        2. Execute action generate sdn state apps nmx-controller type nv-bridge-client-state
        3. Verify the action succeeds
        4. Verify state file is generated
    """
    TestToolkit.tested_api = test_api
    cluster = Cluster()
    sdn = Sdn()
    sdnFiles = sdn.state.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[
        SystemConsts.NV_BRIDGE_CLIENT_STATE]
    try:
        with allure.step("Enable cluster"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=OutputFormat.json),
                output_format=OutputFormat.json).get_returned_value()

            if output[SystemConsts.STATE] == SystemConsts.CLUSTER_STATE_DISABLED:
                cluster.set(op_param_name=SystemConsts.STATE, op_param_value=SystemConsts.CLUSTER_STATE_ENABLED, apply=True)
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state=SystemConsts.CLUSTER_STATE_ENABLED,
                                                                 nmx_c_expected_state=SystemConsts.CLUSTER_APP_STATE_UP)

        with allure.step("Configure nv-bridge nodes"):
            cluster.node.primary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER, op_param_value=engines.dut.ip,
                                                  apply=True, dut_engine=engines.dut)

        with allure.step("Wait for nv-bridge to be configured"):
            _verify_nv_bridge_output(nv_command.system.nv_bridge, state=SystemConsts.NV_BRIDGE_ENABLED,
                                     dut_engine=engines.dut, connections=engines.dut.ip,
                                     local_host=True)
            show_data = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.nv_bridge.show()).get_returned_value()

        with allure.step("Generate and verify nv-bridge-client-state file"):
            sdnFiles.files.delete_all_existing_files()
            _generate_and_verify_state_file(sdnFiles, show_data, engine=engines.dut)

    finally:
        with allure.step("Delete generated files"):
            sdnFiles.files.delete_all_existing_files()
        with allure.step("Disable cluster"):
            cluster.unset(apply=True, dut_engine=engines.dut)


@disabled_access_ports
@pytest.mark.nv_bridge
@pytest.mark.system
def test_system_nv_bridge_primary_secondary(engines, nv_command, random_api, topology_obj, has_loopbox, standalone_system, devices, setup_name, is_simx):
    """
    Test to cover bug nvbugs/5949850
    Test flow:
        1. Enable cluster if not enabled
        2. Set cluster node for primary and secondary
        3. Wait for cluster apps in ok state
        4. Disable cluster
    """
    output_format = OutputFormat.json
    cluster = Cluster()
    try:
        with allure.step("Get all eth ip addresses"):
            dut_setup_specific_attributes: Dict[str, str] = \
                topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']
            setup_mgmt_ips = [dut_setup_specific_attributes['ip_address'], dut_setup_specific_attributes['ip_address_2']]
        with allure.step("Enable cluster"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=OutputFormat.json),
                output_format=OutputFormat.json).get_returned_value()

            if output[SystemConsts.STATE] == SystemConsts.CLUSTER_STATE_DISABLED:
                cluster.set(op_param_name=SystemConsts.STATE, op_param_value=SystemConsts.CLUSTER_STATE_ENABLED, apply=True)
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state=SystemConsts.CLUSTER_STATE_ENABLED,
                                                                 nmx_c_expected_state=SystemConsts.CLUSTER_APP_STATE_UP)

        with allure.step("Configure nv-bridge nodes"):
            cluster.node.primary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER, op_param_value=setup_mgmt_ips[0],
                                                  apply=True, dut_engine=engines.dut)
            cluster.node.secondary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER,
                                                    op_param_value=setup_mgmt_ips[1],
                                                    apply=True, dut_engine=engines.dut)

        with allure.step("Wait for nv-bridge to be configured"):
            eth0_ip = Port('eth0').interface.ipv4.get_primary_ip_address(dut_engine=engines.dut)
            _verify_nv_bridge_output(nv_command.system.nv_bridge, state=SystemConsts.NV_BRIDGE_ENABLED,
                                     dut_engine=engines.dut, connections=eth0_ip, local_host=True)

        with allure.step("Wait for cluster apps in ok state"):
            if not standalone_system:
                ClusterTools.wait_for_app_healthy(cluster, ClusterApps.NMX_CONTROLLER, engine=engines.dut)
            ClusterTools.wait_for_app_healthy(cluster, ClusterApps.NMX_TELEMETRY, engine=engines.dut)
            TestToolkit.tested_api = ApiType.NVUE
            ClusterTools.verify_apps_running(engines.dut, devices, cluster, 'ok', output_format, standalone_system,
                                             has_loopbox, is_simx=is_simx)
            TestToolkit.tested_api = random_api

    finally:
        with allure.step("Disable cluster"):
            cluster.unset(apply=True, dut_engine=engines.dut)


@retry(Exception, tries=20, delay=5)
def _verify_nv_bridge_output(nv_bridge, state=None, health=None, health_reason=None, connections=None, no_connection=None, heart_beat=None, dut_engine=None, active=None, client_address=None, client_id=None, server_address=None, local_host=None):
    output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(nv_bridge.show(dut_engine=dut_engine)).get_returned_value()
    if state:
        ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.NV_BRIDGE_STATE, state).verify_result()
    if health:
        ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.NV_BRIDGE_HEALTH, health).verify_result()
    if health_reason:
        ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.NV_BRIDGE_HEALTH_REASON, health_reason).verify_result()
    if connections:
        assert output_dictionary['connections'], 'No nv-bridge connection established'
        if active and client_address and client_id and server_address:
            assert active in output_dictionary['connections']['1'][SystemConsts.NV_BRIDGE_CLIENT_ACTIVE], f'Connection is not {active}'
            assert client_address in output_dictionary['connections']['1'][SystemConsts.NV_BRIDGE_CLIENT_ADDRESS], f'Client-address {client_address} not in output'
            assert client_id in output_dictionary['connections']['1'][SystemConsts.NV_BRIDGE_CLIENT_ID], f'Client-id {client_id} not in server address'
            assert server_address in output_dictionary['connections']['1'][SystemConsts.NV_BRIDGE_SERVER_ADDRESS], f'Server-address {server_address} not in output'
        if local_host:
            assert connections in output_dictionary['connections']['1'][SystemConsts.NV_BRIDGE_CLIENT_ADDRESS], f'Node ip {connections} not in client address'
            try:
                assert connections in output_dictionary['connections']['1'][SystemConsts.NV_BRIDGE_SERVER_ADDRESS], f'Node ip {connections} not in server address'
            except Exception:
                assert dut_engine.ip in output_dictionary['connections']['1'][SystemConsts.NV_BRIDGE_SERVER_ADDRESS], f'Node ip {dut_engine.ip} in server address'
        if heart_beat:
            ValidationTool.verify_field_value_in_output(output_dictionary['connections']['1'], SystemConsts.NV_BRIDGE_HEARTBEAT, heart_beat).verify_result()
    if no_connection:
        assert output_dictionary['connections'] == {}, 'Connections not empty'


# nv-bridge reports its state to nmx-controller every 10 seconds,
# so the first generated files may not reflect the connection yet
@retry(Exception, tries=5, delay=2)
def _generate_and_verify_state_file(sdn_files, show_data, engine):
    with allure.step("Generate nv-bridge-client-state file"):
        sdn_files.action_generate_sdn().verify_result()

        output = OutputParsingTool.parse_show_output_to_dict(
            sdn_files.files.show(output_format=OutputFormat.json),
            output_format=OutputFormat.json).get_returned_value()
        assert output, "Expected nv-bridge-client-state file to be generated"

    with allure.step("Verify file content"):
        # file names are timestamped, so the highest name is the newest file
        # The file names end in YYYYMMDD_HHMMSS, which sorts correctly as a plain string, so max() on the keys is enough.
        file_path = output[max(output)]["path"]
        file_content = engine.run_cmd(f"sudo cat {file_path}")
        assert file_content, "State file content is empty"

    msg = f"File content is invalid. content: {file_content}"

    with allure.step("Verify hostname in state file"):
        hostname = engine.run_cmd("hostname")
        assert f'"nvbdUuid": "{hostname}' in file_content, msg

    if show_data.get("connections"):
        with allure.step("Verify server id in state file"):
            server_id = show_data.get("server-id", "")
            assert f'"nvbsUuid": "{server_id}"' in file_content, msg

        with allure.step("Verify server address in state file"):
            conn = next(iter(show_data["connections"].values()))
            server_address = conn.get(SystemConsts.NV_BRIDGE_SERVER_ADDRESS, "")
            assert f'"nvbsAddress": "{server_address}"' in file_content, msg


def _send_mad_verify_no_errors(dut, tray_index):
    cmd = 'apt install -y procps vim openssh-client infiniband-diags less iputils-ping'
    cmd += ' ; ' + 'export NV_BRIDGE_CLIENT_MODE=bridge NV_BRIDGE_LOG_LEVEL=info LD_PRELOAD=/usr/local/nv-bridge/lib/libnv_bridge.so'
    cmd += ' ; ' + f'smpquery ni -L 1 -C nvbs-{tray_index}-ca-01'
    output = dut.run_cmd(f"docker exec -it nmx-c-job.nmx-c-group.nmxc bash -c '{cmd}'")
    assert 'SystemGuid' in output, 'MAD not sent, SystemGuid was not found in output'


def _restart_nv_bridge_container(dut):
    dut.run_cmd('sudo systemctl restart nv-bridge.service')
