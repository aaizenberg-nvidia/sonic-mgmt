import logging
import time
import pytest
import random

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.system.grpc_tunnel.constants import GrpcTunnelConstants
from ngts.tests_nvos.system.grpc_tunnel.grpcTunnelServer import GrpcTunnelServer
from ngts.tests_nvos.system.grpc_tunnel.helpers import (
    GrpcTunnelExpectation,
    _without_established,
    build_expected_tunnel_output,
    build_expected_tunnels_map,
    create_grpc_tunnels,
    delete_tunnel_collectors,
    prepare_tunnel_collectors,
    stop_tunnel_subscriptions,
    subscribe_tunnel_collectors,
    validate_grpc_tunnel_docker_ps,
)

logger = logging.getLogger()


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.grpc_tunnel
def test_grpc_tunnel_bad_cli_values(engines, topology_obj):
    """
    Check gnmi basic flow: show command , disable and enable commands, validate stream updates to gnmi-client,
     with subscribe mode - poll.
        Test flow:
            - nv set system grpc-tunnel server <150 x 'a'> (more than 128 chars)
                - Error: name is too long
            - nv set system grpc-tunnel server <name> address _temp/server@12 (not ipv4 or ipv6 or hostname)
                - Error: 'server@12' is not a 'idn-hostname'. Hostname could not be encoded. Character '@' at position 7 is not allowed.
                - Error: 'server@12' is not a 'ipv6'. illegal IP address string passed to inet_pton (may repeat)
                - Error: 'server@12' is not a 'ipv4'. IPv4 address is a.b.c.d where all numbers are between 0 and 255.
            - nv set system grpc-tunnel server <name> port 66000 (> 65535)
                - Error: Valid range for port is 1 - 65535
            - nv set system grpc-tunnel server <name> target-type <invalid> (not GNMI-GNOI)
                - Error: <invalid> is not one of ['gnmi-gnoi']
            - nv set system grpc-tunnel server <name> state DISABLED ( not enabled/disabled )
                - Error: 'DISABLED' is not one of ['enabled', 'disabled']
            - nv set system grpc-tunnel server <name> retry-interval -5/5/301 (<10 or >300)
                - Error: Valid range for retry-interval is 10 - 300
    """
    system = System()
    with allure.step("test grpc tunnel bad cli values"):
        with allure.independent_step("test too long server name"):
            server_name_150_chars = 'a' * 150
            error_message = f"'{server_name_150_chars}' is too long"
            system.grpc_tunnel.server.set(server_name_150_chars).verify_result(should_succeed=False, expected_value=error_message)

        with allure.independent_step("test invalid address"):
            invalid_address = 'server@12'
            expected_address_errors = (
                f"Error: '{invalid_address}' is not a 'idn-hostname'. Hostname could not be encoded. "
                f"Character '@' at position 7 is not allowed.",
                f"Error: '{invalid_address}' is not a 'ipv4'. IPv4 address is a.b.c.d where all numbers are between 0 and 255.",
            )
            expected_ipv6_errors = (
                f"Error: '{invalid_address}' is not a 'ipv6'. illegal IP address string passed to inet_pton",
                f"Error: '{invalid_address}' is not a 'ipv6'. At least 3 parts expected in '{invalid_address}'",
            )
            output = system.grpc_tunnel.server.tunnel_name['testing'].set(
                op_param_name=GrpcTunnelConstants.ADDRESS, op_param_value=invalid_address
            ).get_returned_value(should_succeed=False)
            missing = [msg for msg in expected_address_errors if msg not in output]
            assert not missing, (
                f'Missing expected error line(s) for invalid address {invalid_address!r}:\n' +
                '\n'.join(missing) +
                f'\n--- output ---\n{output}'
            )
            assert any(e in output for e in expected_ipv6_errors), (
                f'Missing ipv6 error for {invalid_address!r}, expected one of:\n' +
                '\n'.join(expected_ipv6_errors) +
                f'\n--- output ---\n{output}'
            )

        with allure.independent_step("test invalid port"):
            invalid_port = 66666
            error_message = f"Error: Valid range for {GrpcTunnelConstants.PORT} is 1 - 65535"
            system.grpc_tunnel.server.tunnel_name['testing'].set(
                op_param_name=GrpcTunnelConstants.PORT, op_param_value=invalid_port
            ).verify_result(should_succeed=False, expected_value=error_message)

        with allure.independent_step("test invalid target type"):
            invalid_target_type = "gnmi-invalid"
            error_message = f"Error: '{invalid_target_type}' is not one of ['gnmi-gnoi']"
            system.grpc_tunnel.server.tunnel_name['testing'].set(
                op_param_name=GrpcTunnelConstants.TARGET_TYPE, op_param_value=invalid_target_type
            ).verify_result(should_succeed=False, expected_value=error_message)

        with allure.independent_step("test invalid target type"):
            invalid_tunnel_state = "DISABLED"
            error_message = f"Error: '{invalid_tunnel_state}' is not one of ['enabled', 'disabled']"
            system.grpc_tunnel.server.tunnel_name['testing'].set(
                op_param_name=GrpcTunnelConstants.STATE, op_param_value=invalid_tunnel_state
            ).verify_result(should_succeed=False, expected_value=error_message)

        with allure.independent_step("test invalid retry interval"):
            invalid_retry_interval = random.choice([-5, 5, 301])
            error_message = f"Error: Valid range for {GrpcTunnelConstants.RETRY_INTERVAL} is 10 - 300"
            system.grpc_tunnel.server.tunnel_name['testing'].set(
                op_param_name=GrpcTunnelConstants.RETRY_INTERVAL, op_param_value=invalid_retry_interval
            ).verify_result(should_succeed=False, expected_value=error_message)
        try:
            with allure.independent_step("test address or port only configured "):
                system.grpc_tunnel.server.tunnel_name['testing'].set(
                    op_param_name=GrpcTunnelConstants.ADDRESS, op_param_value='10.1.1.1', apply=True
                ).verify_result(should_succeed=False, expected_value="Address and port must be set for configured servers")
                logging.info("Detaching any unapplied config")
                NvueGeneralCli.detach_config(engines.dut)
                system.grpc_tunnel.server.tunnel_name['testing'].set(
                    op_param_name=GrpcTunnelConstants.PORT, op_param_value='1234', apply=True
                ).verify_result(should_succeed=False, expected_value="Address and port must be set for configured servers")
        finally:
            logging.info("Detaching any unapplied config")
            NvueGeneralCli.detach_config(engines.dut)


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.grpc_tunnel
def test_grpc_tunnel_basic_flow_stream(engines, topology_obj):
    """
    Validate basic lifecycle of a single gRPC tunnel server object: create, enable, verify connection, disable,
    unset, and verify NVUE show outputs reflect the correct operational and applied state.

        Test flow:
            1. nv show system grpc-tunnel
            2. nv show system grpc-tunnel server
            3. output is No Data for both
            4. Config new grpc-tunnel using setup player as server
            5. nv show system grpc-tunnel
            6. nv show system grpc-tunnel server
            7. nv show system grpc-tunnel server <name>
            8. nv show system grpc-tunnel server <name> status
            9. nv show system grpc-tunnel server <name> connection
    """
    system = System()
    with allure.step("test grpc tunnel basic flow stream"):
        with allure.independent_step("test default output"):
            validate_all_show_commands_output(system)

        with allure.step("test new grpc-tunnel"):
            server = engines['sonic_mgmt']
            new_tunnel_name = 'testing'
            port = "1234"
            with allure.independent_step(f"test set system grpc-tunnel server - {new_tunnel_name}"):
                system.grpc_tunnel.server.set_new_tunnel(tunnel_name=new_tunnel_name)
                system.grpc_tunnel.server.tunnel_name[new_tunnel_name].set(op_param_name=GrpcTunnelConstants.ADDRESS, op_param_value=server.ip)
                system.grpc_tunnel.server.tunnel_name[new_tunnel_name].set(op_param_name=GrpcTunnelConstants.PORT, op_param_value=port, apply=True)

        with allure.step("create tunnel server on sonic_mgmt (gnmic collector)"):
            tunnel_collector = GrpcTunnelServer(
                username='admin',
                password='admin',
                tunnel_address=f':{port}',
                tunnel_name=new_tunnel_name,
                server=server,
            )
            try:
                tunnel_collector.prepare_with_tls()
                tunnel_collector.subscribe(
                    path='/system/state/boot-time',
                    keep_subscription_alive=True,
                    first_update_timeout=30,
                )
                assert tunnel_collector.subscribe_first_update_seconds is not None
                logger.info(
                    'first subscribe update after %.2fs',
                    tunnel_collector.subscribe_first_update_seconds,
                )
                assert tunnel_collector.subscribe_first_update_seconds < GrpcTunnelConstants.SUBSCRIPTION_THRESHOLD, f"Subscribe first update timeout is not as expected: {tunnel_collector.subscribe_first_update_seconds}"
                validate_all_show_commands_output(system, new_tunnel_name, port, server.ip, "yes")
            finally:
                with allure.independent_step("validate show commands output after stop subscription"):
                    tunnel_collector.stop_subscription()
                    validate_all_show_commands_output(system, new_tunnel_name, port, server.ip, "no")
                with allure.independent_step("validate show commands output after delete tunnels and unset tunnel server"):
                    tunnel_collector.delete()
                    system.grpc_tunnel.server.tunnel_name[new_tunnel_name].unset(apply=True).get_returned_value(should_succeed=True)
                    validate_all_show_commands_output(system)


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(8 * MINUTE, func_only=True)
@pytest.mark.grpc_tunnel
def test_grpc_tunnel_stress_flow(engines, topology_obj):
    """
        This test exercises the grpc tunnel "stress flow" lifecycle by:
      - Creating the maximum allowed number of tunnels (10)
      - Checking outputs of system show commands for correct state
      - Testing subscribe and unsubscribe flows across multiple tunnels
      - Asserting that creation of more than 10 tunnels fails as expected

    Test flow:
      1. Create 10 tunnels, each with a unique random name and port
      2. Validate output of the tunnel-related show commands
      3. Attempt to create an 11th tunnel and check for proper error reporting
      4. Test subscribe functionality to ensure events are received for a subset of tunnels
      5. When multiple subscriptions are active, confirm first update timing for each is as expected
      6. Delete all created tunnels
      7. Check that show outputs now report no tunnels
      8. Again validate subscribe and unsubscribe flows post-deletion
      9. Confirm, by retry, that no more than 10 tunnels can be created at any time
    """
    system = System()
    with allure.step("test grpc tunnel stress flow"):
        with allure.independent_step("test create 10 tunnels and validate the output"):
            server = engines['sonic_mgmt']
            tunnel_setups, all_tunnel_expectations = create_grpc_tunnels(system, server)
            with allure.step(
                f"wait {GrpcTunnelConstants.STRESS_TUNNEL_SETTLE_WAIT_SEC}s for tunnel state to settle"
            ):
                time.sleep(GrpcTunnelConstants.STRESS_TUNNEL_SETTLE_WAIT_SEC)
            validate_all_show_commands_output(
                system,
                expected_tunnels=build_expected_tunnels_map(all_tunnel_expectations),
            )

        with allure.independent_step("test create 11th tunnel and validate the error message"):
            new_tunnel_name = "testing_10"
            tunnel_listen_port = random.randint(49152, 65535)
            system.grpc_tunnel.server.set_new_tunnel(tunnel_name=new_tunnel_name)
            system.grpc_tunnel.server.tunnel_name[new_tunnel_name].set(
                op_param_name=GrpcTunnelConstants.ADDRESS, op_param_value=server.ip
            )
            error_message = "Only ten gRPC Tunnels can be configured at one time."
            output = system.grpc_tunnel.server.tunnel_name[new_tunnel_name].set(
                op_param_name=GrpcTunnelConstants.PORT,
                op_param_value=str(tunnel_listen_port),
                apply=True,
            ).get_returned_value(should_succeed=False)
            assert error_message in output, f"Error message is not as expected: {error_message}"
            logging.info("Detaching any unapplied config")
            NvueGeneralCli.detach_config(engines.dut)
        # Prepare every collector; start subscribe only on a subset (pass indices=None for all).
        subscribe_indices = [0, 1]
        try:
            with allure.step("start subscribe on a subset of tunnels"):
                prepare_tunnel_collectors(tunnel_setups)
                subscribe_tunnel_collectors(
                    tunnel_setups,
                    indices=subscribe_indices,
                    path='/system/state/boot-time',
                    keep_subscription_alive=True,
                    first_update_timeout=120,
                )
                for idx in subscribe_indices:
                    assert tunnel_setups[idx].collector.subscribe_first_update_seconds is not None
                    logger.info(
                        'tunnel %s first flat update after %.2fs',
                        tunnel_setups[idx].tunnel_name,
                        tunnel_setups[idx].collector.subscribe_first_update_seconds,
                    )
                subscribed_names = {tunnel_setups[idx].tunnel_name for idx in subscribe_indices}
                post_subscribe_expectations = [
                    GrpcTunnelExpectation(
                        tunnel_name=e.tunnel_name,
                        address=e.address,
                        port=e.port,
                        connection_status="yes" if e.tunnel_name in subscribed_names else "no",
                    )
                    for e in all_tunnel_expectations
                ]
                with allure.step("validate show commands output after subscribe"):
                    validate_all_show_commands_output(
                        system,
                        expected_tunnels=build_expected_tunnels_map(post_subscribe_expectations),
                    )
        finally:
            stop_tunnel_subscriptions(tunnel_setups)
            validate_all_show_commands_output(
                system,

                expected_tunnels=build_expected_tunnels_map(all_tunnel_expectations),
            )
            delete_tunnel_collectors(tunnel_setups)
            for s in tunnel_setups:
                system.grpc_tunnel.server.tunnel_name[s.tunnel_name].unset(apply=True).get_returned_value(
                    should_succeed=True
                )
            validate_all_show_commands_output(system)


def validate_all_show_commands_output(
    system,
    new_tunnel_name=None,
    port=None,
    address=None,
    connection_status=None,
    expected_tunnels=None,
    engine=None,
):
    with allure.step("test all show commands output and grpc tunnel dockers"):
        time.sleep(15)
        if expected_tunnels is None and new_tunnel_name is not None:
            expected_tunnels = {
                new_tunnel_name: build_expected_tunnel_output(
                    address=address,
                    port=int(port),

                    connection_status=connection_status,
                )
            }
        dut_engine = engine or TestToolkit.get_engine()

        with allure.independent_step("test show commands"):
            if not expected_tunnels:
                with allure.independent_step("test show system grpc-tunnel - empty output"):
                    tunnel_show_output = system.grpc_tunnel.show()
                    assert '"server": {}' in tunnel_show_output, f"Tunnel show output is not as expected: {tunnel_show_output}"

                with allure.independent_step("test show system grpc-tunnel server - empty output"):
                    server_show_output = system.grpc_tunnel.server.show()
                    assert "{}" in server_show_output, f"Server show output is not as expected: {server_show_output}"
            else:
                with allure.independent_step("test show system grpc-tunnel"):
                    tunnel_show_output = OutputParsingTool.parse_show_output_to_dict(
                        system.grpc_tunnel.show()
                    ).get_returned_value()
                    expected_tunnel_top = {"server": expected_tunnels}
                    assert _without_established(tunnel_show_output) == _without_established(expected_tunnel_top), (
                        f"show command mismatch:\n{tunnel_show_output!r}\n!=\n{expected_tunnel_top!r}"
                    )

                with allure.independent_step("test show system grpc-tunnel server"):
                    server_list = OutputParsingTool.parse_show_output_to_dict(
                        system.grpc_tunnel.server.show()
                    ).get_returned_value()
                    expected_server_list = expected_tunnels
                    assert _without_established(server_list) == _without_established(expected_server_list), (
                        f"show command mismatch:\n{server_list!r}\n!=\n{expected_server_list!r}"
                    )

                # Per-tunnel: JSON is the inner object only (no {"<name>": {...}} wrapper).
                for tunnel_name, expected_output in expected_tunnels.items():
                    with allure.independent_step(f"test show system grpc-tunnel server {tunnel_name}"):
                        server_show_output = OutputParsingTool.parse_show_output_to_dict(
                            system.grpc_tunnel.server.tunnel_name[tunnel_name].show()
                        ).get_returned_value()
                        assert _without_established(server_show_output) == _without_established(expected_output), (
                            f"show command mismatch:\n{server_show_output!r}\n!=\n{expected_output!r}"
                        )

                    with allure.independent_step(f"test show system grpc-tunnel server {tunnel_name} status"):
                        status_out = OutputParsingTool.parse_show_output_to_dict(
                            system.grpc_tunnel.server.tunnel_name[tunnel_name].status.show()
                        ).get_returned_value()
                        assert _without_established(status_out) == _without_established(expected_output["status"]), (
                            f"show command mismatch:\n{status_out!r}\n!=\n{expected_output['status']!r}"
                        )

                    with allure.independent_step(
                        f"test show system grpc-tunnel server {tunnel_name} "
                        f"status {GrpcTunnelConstants.CONNECTION}"
                    ):
                        conn_out = OutputParsingTool.parse_show_output_to_dict(
                            system.grpc_tunnel.server.tunnel_name[tunnel_name].status.show(
                                GrpcTunnelConstants.CONNECTION
                            )
                        ).get_returned_value()
                        assert _without_established(conn_out) == _without_established(
                            expected_output["status"]["connection"]
                        ), (
                            f"show command mismatch:\n{conn_out!r}\n!=\n"
                            f"{expected_output['status']['connection']!r}"
                        )

        tunnel_names = list(expected_tunnels.keys()) if expected_tunnels else None
        with allure.independent_step("validate grpc tunnel docker ps on DUT"):
            validate_grpc_tunnel_docker_ps(dut_engine, tunnel_names=tunnel_names)
