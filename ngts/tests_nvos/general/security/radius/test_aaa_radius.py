import random
import pytest

from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.constants import MAX_TEST_TIMEOUT
from ngts.tests_nvos.general.security.radius.constants import RadiusConsts, RadiusVmServer, RadiusPhysicalServer
from ngts.tests_nvos.general.security.radius.radius_test_utils import update_radius_server_auth_type, \
    get_two_different_radius_servers
from ngts.tests_nvos.general.security.security_test_tools.generic_remote_aaa_testing.generic_remote_aaa_testing import *
from ngts.tests_nvos.general.security.security_test_tools.resource_utils import configure_resource
from ngts.tests_nvos.general.security.security_test_tools.switch_authenticators import SshAuthenticator
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tools.test_utils import allure_utils as allure


@pytest.fixture(scope='session', autouse=True)
def prepare_scp_test(prepare_scp):
    return


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.nvos_chipsim_ci
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_radius_set_unset_show(test_api, engines):
    radius_obj = System().aaa.radius
    generic_aaa_test_set_unset_show(
        test_api=test_api, engines=engines,
        remote_aaa_type=RemoteAaaType.RADIUS,
        main_resource_obj=radius_obj,
        confs={
            radius_obj: {
                AaaConsts.AUTH_TYPE: random.choice(RadiusConsts.VALID_VALUES[AaaConsts.AUTH_TYPE]),
                AaaConsts.PORT: random.choice(RadiusConsts.VALID_VALUES[AaaConsts.PORT]),
                AaaConsts.RETRANSMIT: random.choice(RadiusConsts.VALID_VALUES[AaaConsts.RETRANSMIT]),
                AaaConsts.STATISTICS: random.choice(RadiusConsts.VALID_VALUES[AaaConsts.STATISTICS]),
                AaaConsts.SECRET: 'alontheking',
                AaaConsts.TIMEOUT: random.choice(RadiusConsts.VALID_VALUES[AaaConsts.TIMEOUT])
            },
        },
        server_conf={
            AaaConsts.AUTH_TYPE: random.choice(RadiusConsts.VALID_VALUES[AaaConsts.AUTH_TYPE]),
            AaaConsts.PORT: random.choice(RadiusConsts.VALID_VALUES[AaaConsts.PORT]),
            AaaConsts.RETRANSMIT: random.choice(RadiusConsts.VALID_VALUES[AaaConsts.RETRANSMIT]),
            AaaConsts.SECRET: 'alontheking',
            AaaConsts.TIMEOUT: random.choice(RadiusConsts.VALID_VALUES[AaaConsts.TIMEOUT]),
            AaaConsts.PRIORITY: 2
        },
        default_confs={
            radius_obj: RadiusConsts.DEFAULT_RADIUS_CONF,
        }
    )


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_radius_set_invalid_param(test_api, engines):
    """
    @summary: Verify failure for invalid param values
    """
    radius_obj = System().aaa.radius
    global_radius_fields = [AaaConsts.AUTH_TYPE, AaaConsts.PORT, AaaConsts.SECRET, AaaConsts.TIMEOUT]
    radius_server_fields = global_radius_fields + [AaaConsts.PRIORITY]
    generic_aaa_test_set_invalid_param(
        test_api=test_api,
        field_is_numeric=RadiusConsts.FIELD_IS_NUMERIC,
        valid_values=RadiusConsts.VALID_VALUES,
        resources_and_fields={
            radius_obj: global_radius_fields,
            radius_obj.server.server_id['1.2.3.4']: radius_server_fields
        }
    )


@pytest.mark.check_log_size
@pytest.mark.timeout(MAX_TEST_TIMEOUT, func_only=True)
@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
@pytest.mark.parametrize('addressing_type', AddressingType.ALL_TYPES)
def test_radius_auth(test_flow, test_api, addressing_type, engines, topology_obj, local_adminuser, request):
    """
    @summary: Basic test to verify authentication and authorization through radius, using all possible auth mediums:
        SSH, OpenApi, rcon, scp.

        Steps:
        1. configure radius server
        2. set radius in authentication order, and set failthrough off
        3. verify only radius user can authenticate
            - verify auth with radius user - expect success
            - verify auth with local user - expect fail
    """
    skip_auth_mediums = []
    radius = System().aaa.radius

    # our vm radius server does not support mschapv2 - all auth types will be tested only on physical server
    server_by_addr_type = {
        AddressingType.IPV4: RadiusPhysicalServer.SERVER_IPV4,  # only physical supports mschap (not sure how to configure it)
        AddressingType.IPV6: RadiusVmServer.SERVER_IPV6,
        AddressingType.DN: RadiusVmServer.SERVER_DN
    }
    test_params = RadiusConsts.AUTH_TYPES if addressing_type == AddressingType.IPV4 else [AaaConsts.PAP, AaaConsts.CHAP]

    generic_aaa_test_auth(test_flow=test_flow, test_api=test_api, addressing_type=addressing_type, engines=engines,
                          topology_obj=topology_obj, local_adminuser=local_adminuser, request=request,
                          remote_aaa_type=RemoteAaaType.RADIUS,
                          remote_aaa_obj=radius,
                          server_by_addr_type=server_by_addr_type,
                          test_param=test_params,
                          test_param_update_func=update_radius_server_auth_type, skip_auth_mediums=skip_auth_mediums)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_radius_bad_secret(test_api, engines, topology_obj):
    """
    @summary: Verify that radius users can't auth when bad/no secret is configured.

        Steps:
        1. configure radius server
        2. set no/blank secret
        3. verify auth - expect fail
        4. set bad secret
        5. verify auth - expect fail
    """
    skip_auth_mediums = []
    radius_server = RadiusPhysicalServer.SERVER_IPV4.copy()
    radius_server.secret = RandomizationTool.get_random_string(6)
    generic_aaa_test_bad_configured_server(test_api, engines, topology_obj,
                                           remote_aaa_type=RemoteAaaType.RADIUS,
                                           remote_aaa_obj=System().aaa.radius,
                                           bad_param_name=AaaConsts.SECRET, bad_configured_server=radius_server,
                                           skip_auth_mediums=skip_auth_mediums)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_radius_bad_port(test_api, engines, topology_obj):
    """
    @summary: Verify that radius users can't auth when bad port is configured.

        Steps:
        1. configure radius server
        2. set bad port
        3. verify auth - expect fail
    """
    skip_auth_mediums = []
    radius_server = RadiusPhysicalServer.SERVER_IPV4.copy()
    radius_server.port = AaaConsts.AAA_SERVER_BAD_PORT
    generic_aaa_test_bad_configured_server(test_api, engines, topology_obj,
                                           remote_aaa_type=RemoteAaaType.RADIUS,
                                           remote_aaa_obj=System().aaa.radius,
                                           bad_param_name=AaaConsts.PORT, bad_configured_server=radius_server,
                                           skip_auth_mediums=skip_auth_mediums)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_radius_unique_priority(test_api, engines, topology_obj):
    """
    @summary: Verify that server priority must be unique

        Steps:
        1. Set 2 servers with different priority - expect success
        2. set another server with existing priority - expect failure

    """
    generic_aaa_test_unique_priority(test_api, remote_aaa_obj=System().aaa.radius)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_radius_priority(test_flow, test_api, engines, topology_obj, request):
    """
    @summary: Verify that auth is done via the top prioritized server

        Steps:
        1. set and prioritize 2 servers
        2. verify auth is done via top prioritized server
        3. advance the lowest prioritized server to be most prioritized
        4. repeat steps 2-3 until reach priority 8 (max)
    """
    skip_auth_mediums = []
    server1, server2 = get_two_different_radius_servers()
    generic_aaa_test_priority(test_flow, test_api, engines, topology_obj, request, remote_aaa_type=RemoteAaaType.RADIUS,
                              remote_aaa_obj=System().aaa.radius, server1=server1, server2=server2,
                              skip_auth_mediums=skip_auth_mediums)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_radius_server_unreachable(test_flow, test_api, engines, topology_obj, local_adminuser, request):
    """
    @summary: Verify that when a server is unreachable, auth is done via next in line
        (next server or authentication method – local)

        Steps:
        1.	Configure server
        2.	Set radius in authentication order and failthrough off
        3.	Make server unreachable
        4.	Verify auth - success only with local user
        5.	Configure secondary prioritized server
        6.	Verify auth – success only with 2nd server user
        7.	Make the 2nd server also unreachable
        8.	Verify auth – success only with local user
        9.	Bring back the first server
        10. Verify auth – success only with top server user
    """
    skip_auth_mediums = []
    server1, server2 = get_two_different_radius_servers()
    generic_aaa_test_server_unreachable(test_flow, test_api, engines, topology_obj, request,
                                        local_adminuser=local_adminuser,
                                        remote_aaa_type=RemoteAaaType.RADIUS,
                                        remote_aaa_obj=System().aaa.radius,
                                        server1=server1, server2=server2, skip_auth_mediums=skip_auth_mediums)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_radius_auth_error(test_flow, test_api, engines, topology_obj, local_adminuser: UserInfo, request):
    """
    @summary: Verify the behavior in case of auth error (username not found or bad credentials).

        In case of auth error (username not found, or bad credentials):
        - if failthrough is off -> fail authentication attempt
        - if failthrough is on  -> check credentials on the next server/auth method.

        Steps:
        1.	Configure radius servers
        2.	Set failthrough off
        3.	Verify auth with 2nd server credentials – expect fail
        4.  Verify auth with local user credentials - expect fail
        5.	Set failthrough on
        6.	Verify auth with 2nd server credentials – expect success
        7.  Verify auth with local user credentials - expect success
    """
    skip_auth_mediums = []
    server1, server2 = get_two_different_radius_servers()
    generic_aaa_test_auth_error(test_flow, test_api, engines, topology_obj, request, local_adminuser=local_adminuser,
                                remote_aaa_type=RemoteAaaType.RADIUS,
                                remote_aaa_obj=System().aaa.radius,
                                server1=server1, server2=server2, skip_auth_mediums=skip_auth_mediums)


# -------------------- FEATURE SPECIFIC TESTS ---------------------


@pytest.mark.timeout(MAX_TEST_TIMEOUT, func_only=True)
@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_radius_timeout(test_api, engines, topology_obj, local_adminuser: UserInfo, cleanup_after_aaa):
    """
    @summary: Verify timeout functionality

        In case that server is not reachable, the client (switch) will wait for respond for <timeout> seconds,
            after which it aborts and fails the attempt.

        Steps:
        1. Set unreachable radius server with some timeout
        2. Make authentication attempt and measure time
        3. Verify respond time >= timeout
        4. Set another unreachable server
        5. Make authentication attempt and measure time
        6. Verify respond time >= sum of timeouts

    """
    TestToolkit.tested_api = test_api

    aaa = System().aaa
    try:
        with allure.step('Set unreachable radius server with some timeout'):
            rand_timeout = random.randint(RadiusConsts.VALID_VALUES[AaaConsts.TIMEOUT][0],
                                          RadiusConsts.VALID_VALUES[AaaConsts.TIMEOUT][-1] // 3)
            logging.info(f'Chosen timeout: {rand_timeout}')
            configure_resource(engines, resource_obj=aaa.radius.server.server_id['1.2.3.4'], conf={
                AaaConsts.TIMEOUT: rand_timeout,
                AaaConsts.SECRET: "xyz",
                AaaConsts.PORT: AaaConsts.AAA_SERVER_BAD_PORT
            })

        with allure.step('Set radius in authentication order and failthrough off'):
            configure_resource(engines, resource_obj=aaa.authentication, conf={
                AuthConsts.ORDER: [AuthConsts.RADIUS, AuthConsts.LOCAL],
                AuthConsts.FAILTHROUGH: AaaConsts.DISABLED
            }, apply=True, verify_apply=False)

        with allure.step('Make authentication attempt and measure time'):
            authenticator = SshAuthenticator(local_adminuser.username, local_adminuser.password, engines.dut.ip)
            _, timestamp1 = authenticator.attempt_login_failure()
            _, timestamp2 = authenticator.attempt_login_success(restart_session_process=False)

        with allure.step(f'Verify respond time >= timeout'):
            assert timestamp2 - timestamp1 >= rand_timeout, f'Timeout was too short. Expected: {rand_timeout}'

        with allure.step('Set another unreachable server with timeout'):
            rand_timeout2 = random.randint(RadiusConsts.VALID_VALUES[AaaConsts.TIMEOUT][0],
                                           RadiusConsts.VALID_VALUES[AaaConsts.TIMEOUT][-1] // 3)
            logging.info(f'Chosen timeout: {rand_timeout2}')
            configure_resource(engines, resource_obj=aaa.radius.server.server_id['2.4.6.8'], conf={
                AaaConsts.PRIORITY: 2,
                AaaConsts.TIMEOUT: rand_timeout2,
                AaaConsts.SECRET: "xyz",
                AaaConsts.PORT: AaaConsts.AAA_SERVER_BAD_PORT
            }, apply=True, verify_apply=False)

        with allure.step('Make authentication attempt and measure time'):
            _, timestamp1 = authenticator.attempt_login_failure()
            _, timestamp2 = authenticator.attempt_login_success(restart_session_process=False)

        with allure.step('Verify respond time >= sum of timeouts'):
            assert timestamp2 - timestamp1 >= rand_timeout + rand_timeout2, \
                f'Timeout was too short. Expected: {rand_timeout + rand_timeout2}'
    finally:
        logging.info('Disconnect local engine for cleanup steps')
        engines.dut.disconnect()

        # with allure.step('Remote reboot'):
        #     NvueGeneralCli(engines.dut).remote_reboot(topology_obj)

        # with allure.step('Clear aaa configuration'):
        #     aaa.unset(apply=True).verify_result()
