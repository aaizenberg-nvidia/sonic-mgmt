from typing import Mapping, Sequence
import logging
import random
import pytest

from ..security_test_tools.constants import AccountingConsts, AuthMode, AddressingType, AaaConsts, AccountingFields, AuthConsts
from ..security_test_tools.generic_remote_aaa_testing import generic_aaa_accounting_testing, generic_remote_aaa_testing
from .constants import TacacsConsts, TacacsDockerServer1, TacacsDockerServer2, TacacsPhysicalServer
from ..security_test_tools.generic_remote_aaa_testing.constants import RemoteAaaType
from ..security_test_tools.tool_classes.RemoteAaaServerInfo import TacacsServerInfo
from ..security_test_tools import resource_utils as security_resource_utils
from ..security_test_tools.switch_authenticators import SshAuthenticator
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_constants.constants_nvos import ApiType, TestFlowType
from ..security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.ngts_types import EnginesT, TopologyT
from .. import constants as security_constants
from . import tacacs_test_utils

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


@pytest.fixture(scope='session', autouse=True)
def prepare_scp_test(prepare_scp: None) -> None:
    return


def _pick_reachable_tacacs_server(
    engines: EnginesT,
    server_by_addr_type: Mapping[str, TacacsServerInfo],
    preferred_addressing_types: Sequence[str],
) -> tuple[TacacsServerInfo, str]:
    '''
    Pick a reachable TACACS server from the list of preferred addressing types.

    Args:
        engines: The engines object.
        server_by_addr_type: A mapping of addressing types to TACACS server info.
        preferred_addressing_types: A list of preferred addressing types.

    Returns:
        A tuple containing the reachable TACACS server and the addressing type.

    Raises:
        AssertionError: If no reachable TACACS server variants are found.
    '''
    for addressing_type in preferred_addressing_types:
        candidate_server = server_by_addr_type[addressing_type].copy()
        if candidate_server.verify_availability(dut_engine=engines.dut).result:
            return candidate_server, addressing_type

    checked_types = ', '.join(preferred_addressing_types)
    raise AssertionError(f'No reachable TACACS server variants found for addressing types: {checked_types}')


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.nvos_chipsim_ci
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_tacacs_set_unset_show(test_api: str, engines: EnginesT) -> None:
    tacacs_obj = System().aaa.tacacs
    generic_remote_aaa_testing.generic_aaa_test_set_unset_show(
        test_api=test_api, engines=engines,
        remote_aaa_type=RemoteAaaType.TACACS,
        main_resource_obj=tacacs_obj,
        confs={
            tacacs_obj: {
                AaaConsts.PORT: random.choice(TacacsConsts.VALID_VALUES[AaaConsts.PORT]),
                # AaaConsts.RETRANSMIT: random.choice(TacacsConsts.VALID_VALUES[AaaConsts.RETRANSMIT]),
                AaaConsts.SECRET: 'alontheking',
                AaaConsts.TIMEOUT: random.choice(TacacsConsts.VALID_VALUES[AaaConsts.TIMEOUT])
            },
            tacacs_obj.accounting: {
                AccountingFields.STATE: random.choice(AccountingConsts.VALUES[AccountingFields.STATE])
            },
            tacacs_obj.authentication: {
                AaaConsts.AUTHENTICATION_MODE: random.choice(TacacsConsts.VALID_VALUES[AaaConsts.AUTHENTICATION_MODE]),
            }
        },
        server_conf={
            AaaConsts.SERVER_AUTH_MODE: random.choice(TacacsConsts.VALID_VALUES[AaaConsts.SERVER_AUTH_MODE]),
            AaaConsts.PORT: random.choice(TacacsConsts.VALID_VALUES[AaaConsts.PORT]),
            # AaaConsts.RETRANSMIT: random.choice(TacacsConsts.VALID_VALUES[AaaConsts.RETRANSMIT]),
            AaaConsts.SECRET: 'alontheking',
            AaaConsts.TIMEOUT: random.choice(TacacsConsts.VALID_VALUES[AaaConsts.TIMEOUT]),
            AaaConsts.PRIORITY: 2
        },
        default_confs={
            tacacs_obj: TacacsConsts.DEFAULT_TACACS_CONF,
            tacacs_obj.accounting: AccountingConsts.DEFAULT,
            tacacs_obj.authentication: TacacsConsts.DEFAULT_TACACS_AUTHENTICATION_CONF
        }
    )


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_tacacs_set_invalid_param(test_api: str, engines: EnginesT) -> None:
    """
    @summary: Verify failure for invalid param values
    """
    tacacs_obj = System().aaa.tacacs
    global_tacacs_fields = [AaaConsts.PORT, AaaConsts.SECRET, AaaConsts.TIMEOUT]
    tacacs_server_fields = global_tacacs_fields + [AaaConsts.PRIORITY, AaaConsts.SERVER_AUTH_MODE]
    generic_remote_aaa_testing.generic_aaa_test_set_invalid_param(
        test_api=test_api,
        field_is_numeric=TacacsConsts.FIELD_IS_NUMERIC,
        valid_values=TacacsConsts.VALID_VALUES,
        resources_and_fields={
            tacacs_obj: global_tacacs_fields,
            tacacs_obj.server.server_id['1.2.3.4']: tacacs_server_fields,
            tacacs_obj.authentication: [AaaConsts.AUTHENTICATION_MODE]
        }
    )


@pytest.mark.check_log_size
@pytest.mark.timeout(security_constants.MAX_TEST_TIMEOUT, func_only=True)
@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
@pytest.mark.parametrize('addressing_type', AddressingType.ALL_TYPES)
def test_tacacs_auth(
    test_flow: str,
    test_api: str,
    addressing_type: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    local_adminuser: UserInfo,
    request: pytest.FixtureRequest,
) -> None:
    """
    @summary: Basic test to verify authentication and authorization through tacacs, using all possible auth mediums:
        SSH, OpenApi, rcon, scp.

        Steps:
        1. configure tacacs server
        2. set tacacs in authentication order, and set failthrough off
        3. verify only tacacs user can authenticate
            - verify auth with tacacs user - expect success
            - verify auth with local user - expect fail
    """
    skip_auth_mediums = []
    tacacs = System().aaa.tacacs
    generic_remote_aaa_testing.generic_aaa_test_auth(test_flow=test_flow, test_api=test_api, addressing_type=addressing_type, engines=engines,
                                                     topology_obj=topology_obj, local_adminuser=local_adminuser, request=request,
                                                     remote_aaa_type=RemoteAaaType.TACACS,
                                                     remote_aaa_obj=tacacs,
                                                     server_by_addr_type=TacacsDockerServer1.SERVER_BY_ADDRESSING_TYPE,
                                                     test_param=AuthMode.ALL_TYPES,
                                                     test_param_update_func=tacacs_test_utils.update_tacacs_server_auth_mode,
                                                     skip_auth_mediums=skip_auth_mediums)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_tacacs_bad_secret(test_api: str, engines: EnginesT, topology_obj: TopologyT) -> None:
    """
    @summary: Verify that tacacs users can't auth when bad/no secret is configured.

        Steps:
        1. configure tacacs server
        2. set no/blank secret
        3. verify auth - expect fail
        4. set bad secret
        5. verify auth - expect fail
    """
    tacacs_server = TacacsPhysicalServer.SERVER_IPV4.copy()
    tacacs_server.secret = RandomizationTool.get_random_string(6)
    generic_remote_aaa_testing.generic_aaa_test_bad_configured_server(test_api, engines, topology_obj,
                                                                      remote_aaa_type=RemoteAaaType.TACACS,
                                                                      remote_aaa_obj=System().aaa.tacacs,
                                                                      bad_param_name=AaaConsts.SECRET, bad_configured_server=tacacs_server)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_tacacs_bad_port(test_api: str, engines: EnginesT, topology_obj: TopologyT) -> None:
    """
    @summary: Verify that tacacs users can't auth when bad port is configured.

        Steps:
        1. configure tacacs server
        2. set bad port
        3. verify auth - expect fail
    """
    tacacs_server = TacacsPhysicalServer.SERVER_IPV4.copy()
    tacacs_server.port = AaaConsts.AAA_SERVER_BAD_PORT
    generic_remote_aaa_testing.generic_aaa_test_bad_configured_server(test_api, engines, topology_obj,
                                                                      remote_aaa_type=RemoteAaaType.TACACS,
                                                                      remote_aaa_obj=System().aaa.tacacs,
                                                                      bad_param_name=AaaConsts.PORT, bad_configured_server=tacacs_server)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_tacacs_unique_priority(test_api: str, engines: EnginesT, topology_obj: TopologyT) -> None:
    """
    @summary: Verify that hostname priority must be unique

        Steps:
        1. Set 2 hostnames with different priority - expect success
        2. set another hostname with existing priority - expect failure

    """
    generic_remote_aaa_testing.generic_aaa_test_unique_priority(test_api, remote_aaa_obj=System().aaa.tacacs)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_tacacs_priority(test_flow: str, test_api: str, engines: EnginesT, topology_obj: TopologyT, request: pytest.FixtureRequest) -> None:
    """
    @summary: Verify that auth is done via the lowest prioritized server

        Steps:
        1. set and prioritize 2 servers
        2. verify auth is done via lowest prioritized server
        3. advance the lowest prioritized server to be most prioritized
        4. repeat steps 2-3 until reach priority 8 (max)
    """
    server1, server2 = tacacs_test_utils.get_two_different_tacacs_servers()
    generic_remote_aaa_testing.generic_aaa_test_priority(test_flow, test_api, engines, topology_obj, request, remote_aaa_type=RemoteAaaType.TACACS,
                                                         remote_aaa_obj=System().aaa.tacacs, server1=server1, server2=server2)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_tacacs_server_unreachable(
    test_flow: str,
    test_api: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    local_adminuser: UserInfo,
    request: pytest.FixtureRequest,
) -> None:
    """
    @summary: Verify that when a server is unreachable, auth is done via next in line
        (next server or authentication method – local)

        Steps:
        1.	Configure server
        2.	Set tacacs in authentication order and failthrough off
        3.	Make server unreachable
        4.	Verify auth - success only with local user
        5.	Configure secondary prioritized server
        6.	Verify auth – success only with 2nd server user
        7.	Make the 2nd server also unreachable
        8.	Verify auth – success only with local user
        9.	Bring back the first server
        10. Verify auth – success only with lowest server user
    """
    server1, server2 = tacacs_test_utils.get_two_different_tacacs_servers()
    generic_remote_aaa_testing.generic_aaa_test_server_unreachable(test_flow, test_api, engines, topology_obj, request,
                                                                   local_adminuser=local_adminuser,
                                                                   remote_aaa_type=RemoteAaaType.TACACS,
                                                                   remote_aaa_obj=System().aaa.tacacs,
                                                                   server1=server1, server2=server2)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_tacacs_auth_error(
    test_flow: str,
    test_api: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    local_adminuser: UserInfo,
    request: pytest.FixtureRequest,
) -> None:
    """
    @summary: Verify the behavior in case of auth error (username not found or bad credentials).

        In case of auth error (username not found, or bad credentials):
        - if failthrough is off -> fail authentication attempt
        - if failthrough is on  -> check credentials on the next server/auth method.

        Steps:
        1.	Configure tacacs servers
        2.	Set failthrough off
        3.	Verify auth with 2nd server credentials – expect fail
        4.  Verify auth with local user credentials - expect fail
        5.	Set failthrough on
        6.	Verify auth with 2nd server credentials – expect success
        7.  Verify auth with local user credentials - expect success
    """
    server1, server2 = tacacs_test_utils.get_two_different_tacacs_servers()
    generic_remote_aaa_testing.generic_aaa_test_auth_error(test_flow, test_api, engines, topology_obj, request, local_adminuser=local_adminuser,
                                                           remote_aaa_type=RemoteAaaType.TACACS,
                                                           remote_aaa_obj=System().aaa.tacacs,
                                                           server1=server1, server2=server2)


# -------------------- FEATURE SPECIFIC TESTS ---------------------

"""
NOTES:
* for accounting - timestamps in accounting files don't include the year, so tests may fail when new year begins.
    therefore, make sure to clean accounting file in the tacacs servers:
    ```docker exec <server-docker-name> bash -c "echo '' > /var/log/tac.acct"```
"""


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
@pytest.mark.parametrize('addressing_type', AddressingType.ALL_TYPES)
def test_tacacs_accounting_basic(
    test_api: str,
    addressing_type: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    request: pytest.FixtureRequest,
    local_adminuser: UserInfo,
    switch_hostname: str,
) -> None:
    """
    @summary: Verify accounting basic functionality

        Steps:
        1. configure tacacs
        2. disable accounting
        3. enable tacacs
        4. verify no accounting logs on server
        5. enable accounting
        6. verify accounting logs on server only for tacacs users events
    """
    skip_auth_mediums = []
    test_server = TacacsDockerServer1.SERVER_BY_ADDRESSING_TYPE[addressing_type].copy()
    test_server.auth_mode = AuthMode.LOGIN
    test_server.users = TacacsDockerServer1.USERS_BY_AUTH_MODE[test_server.auth_mode]

    with allure.step('Remove leftover TACACS server entries from previous tests'):
        engines.dut.run_cmd('nv config detach', validate=False)
        engines.dut.run_cmd('nv unset system aaa tacacs server', validate=False)
        engines.dut.run_cmd('nv config apply -y', validate=False)

    generic_aaa_accounting_testing.generic_aaa_test_accounting_basic(test_api, engines, topology_obj, request, switch_hostname, local_adminuser,
                                                                     remote_aaa_type=RemoteAaaType.TACACS,
                                                                     remote_aaa_obj=System().aaa.tacacs,
                                                                     server=test_server, skip_auth_mediums=skip_auth_mediums)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_tacacs_accounting_lowest_server_only(
    test_api: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    request: pytest.FixtureRequest,
    local_adminuser: UserInfo,
    switch_hostname: str,
) -> None:
    """
    @summary: Verify that accounting logs are sent to lowest server only

        Steps:
        1. configure tacacs with 2 servers
        2. enable accounting
        3. enable tacacs
        4. verify accounting logs on lowest server only for tacacs users events
    """
    auth_mode1 = random.choice(AuthMode.ALL_TYPES)
    auth_mode2 = random.choice(AuthMode.ALL_TYPES)
    addressing_types = list(AddressingType.ALL_TYPES)
    random.shuffle(addressing_types)

    test_server1, addressing_type1 = _pick_reachable_tacacs_server(
        engines,
        TacacsDockerServer1.SERVER_BY_ADDRESSING_TYPE,
        addressing_types,
    )
    # Prefer distinct address forms when available, but fall back to the same type if the lab only reaches one variant.
    addressing_types_for_server2 = [addr_type for addr_type in addressing_types if addr_type != addressing_type1]
    addressing_types_for_server2.append(addressing_type1)
    test_server2, _ = _pick_reachable_tacacs_server(
        engines,
        TacacsDockerServer2.SERVER_BY_ADDRESSING_TYPE,
        addressing_types_for_server2,
    )

    test_server1.priority = 1
    test_server2.priority = 2

    test_server1.auth_mode = AuthMode.LOGIN
    test_server1.users = TacacsDockerServer1.USERS_BY_AUTH_MODE[AuthMode.LOGIN]
    test_server2.auth_mode = AuthMode.LOGIN
    test_server2.users = TacacsDockerServer2.USERS_BY_AUTH_MODE[AuthMode.LOGIN]

    with allure.step('Remove leftover TACACS server entries from previous tests'):
        engines.dut.run_cmd('nv config detach', validate=False)
        engines.dut.run_cmd('nv unset system aaa tacacs server', validate=False)
        engines.dut.run_cmd('nv config apply -y', validate=False)

    generic_aaa_accounting_testing.generic_aaa_test_accounting_lowest_server_only(test_api, engines, topology_obj, request, switch_hostname,
                                                                                  local_adminuser,
                                                                                  remote_aaa_type=RemoteAaaType.TACACS,
                                                                                  remote_aaa_obj=System().aaa.tacacs,
                                                                                  lowest_server=test_server1, highest_server=test_server2)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_tacacs_accounting_unreachable_lowest_server(
    test_api: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    request: pytest.FixtureRequest,
    local_adminuser: UserInfo,
    switch_hostname: str,
) -> None:
    """
    @summary: Verify that when lowest server becomes unreachable, accounting logs are sent to next available server only

        Steps:
        1. configure tacacs with several lowest unreachable servers
        2. configure also reachable server with lower priority
        3. enable accounting
        4. enable tacacs
        5. verify accounting logs on lowest available server only for tacacs users events
        6. make unreachable server reachable
        7. verify accounting logs now on the lowest reachable server
    """
    addressing_type1 = random.choice(AddressingType.ALL_TYPES)
    addressing_type2 = RandomizationTool.select_random_value(AddressingType.ALL_TYPES,
                                                             [addressing_type1]).get_returned_value()

    test_server1 = TacacsDockerServer1.SERVER_BY_ADDRESSING_TYPE[addressing_type1].copy()
    test_server2 = TacacsDockerServer2.SERVER_BY_ADDRESSING_TYPE[addressing_type2].copy()

    test_server1.priority = 1
    test_server2.priority = 2

    test_server1.auth_mode = AuthMode.LOGIN
    test_server1.users = TacacsDockerServer1.USERS_BY_AUTH_MODE[AuthMode.LOGIN]
    test_server2.auth_mode = AuthMode.LOGIN
    test_server2.users = TacacsDockerServer2.USERS_BY_AUTH_MODE[AuthMode.LOGIN]

    with allure.step('Remove leftover TACACS server entries from previous tests'):
        engines.dut.run_cmd('nv config detach', validate=False)
        engines.dut.run_cmd('nv unset system aaa tacacs server', validate=False)
        engines.dut.run_cmd('nv config apply -y', validate=False)

    generic_aaa_accounting_testing.generic_aaa_test_accounting_unreachable_lowest_server(test_api, engines, topology_obj, request, switch_hostname,
                                                                                         local_adminuser,
                                                                                         remote_aaa_type=RemoteAaaType.TACACS,
                                                                                         remote_aaa_obj=System().aaa.tacacs,
                                                                                         lowest_server=test_server1, highest_server=test_server2)


@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_tacacs_accounting_local_first(
    test_api: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    request: pytest.FixtureRequest,
    local_adminuser: UserInfo,
    switch_hostname: str,
) -> None:
    """
    @summary: Verify that when lowest server becomes unreachable, accounting logs are sent to next available server only

        Steps:
        1. configure tacacs with several lowest unreachable servers
        2. configure also reachable server with lower priority
        3. enable accounting
        4. enable tacacs
        5. verify accounting logs on lowest available server only for tacacs users events
        6. make unreachable server reachable
        7. verify accounting logs now on the lowest reachable server
    """
    addressing_type = random.choice(AddressingType.ALL_TYPES)

    test_server = TacacsDockerServer1.SERVER_BY_ADDRESSING_TYPE[addressing_type].copy()
    test_server.auth_mode = AuthMode.LOGIN
    test_server.users = TacacsDockerServer1.USERS_BY_AUTH_MODE[AuthMode.LOGIN]

    with allure.step('Remove leftover TACACS server entries from previous tests'):
        engines.dut.run_cmd('nv config detach', validate=False)
        engines.dut.run_cmd('nv unset system aaa tacacs server', validate=False)
        engines.dut.run_cmd('nv config apply -y', validate=False)

    generic_aaa_accounting_testing.generic_aaa_test_accounting_local_first(test_api, engines, topology_obj, request, switch_hostname, local_adminuser,
                                                                           remote_aaa_type=RemoteAaaType.TACACS,
                                                                           remote_aaa_obj=System().aaa.tacacs,
                                                                           server=test_server)


@pytest.mark.timeout(security_constants.MAX_TEST_TIMEOUT, func_only=True)
@pytest.mark.security
@pytest.mark.simx_security
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_tacacs_timeout(test_api: str, engines: EnginesT, topology_obj: TopologyT, local_adminuser: UserInfo, cleanup_after_aaa):
    """
    @summary: Verify timeout functionality

        In case that server is not reachable, the client (switch) will wait for respond for <timeout> seconds,
            after which it aborts and fails the attempt.

        Steps:
        1. Set unreachable tacacs server with some timeout
        2. Make authentication attempt and measure time
        3. Verify respond time >= timeout
        4. Set another unreachable server
        5. Make authentication attempt and measure time
        6. Verify respond time >= sum of timeouts

    """
    TestToolkit.tested_api = test_api

    aaa = System().aaa
    try:
        with allure.step('Set unreachable tacacs server with some timeout'):
            rand_timeout = random.randint(TacacsConsts.VALID_VALUES[AaaConsts.TIMEOUT][0],
                                          TacacsConsts.VALID_VALUES[AaaConsts.TIMEOUT][-1] // 3)
            logger.info(f'Chosen timeout: {rand_timeout}')
            security_resource_utils.configure_resource(engines, resource_obj=aaa.tacacs.server.server_id['1.2.3.4'], conf={
                AaaConsts.TIMEOUT: rand_timeout,
                AaaConsts.SECRET: "xyz",
                AaaConsts.PORT: AaaConsts.AAA_SERVER_BAD_PORT
            })

        with allure.step('Set tacacs in authentication order and failthrough off'):
            security_resource_utils.configure_resource(engines, resource_obj=aaa.authentication, conf={
                AuthConsts.ORDER: [AuthConsts.TACACS, AuthConsts.LOCAL],
                AuthConsts.FAILTHROUGH: AaaConsts.DISABLED
            }, apply=True, verify_apply=False)

        with allure.step('Make authentication attempt and measure time'):
            authenticator = SshAuthenticator(local_adminuser.username, local_adminuser.password, engines.dut.ip)
            _, timestamp1 = authenticator.attempt_login_failure()
            _, timestamp2 = authenticator.attempt_login_success(restart_session_process=False)

        with allure.step('Verify respond time >= timeout'):
            assert timestamp2 - timestamp1 >= rand_timeout, f'Timeout was too short. Expected: {rand_timeout}'

        with allure.step('Set another unreachable server with timeout'):
            rand_timeout2 = random.randint(TacacsConsts.VALID_VALUES[AaaConsts.TIMEOUT][0],
                                           TacacsConsts.VALID_VALUES[AaaConsts.TIMEOUT][-1] // 3)
            logger.info(f'Chosen timeout: {rand_timeout2}')
            security_resource_utils.configure_resource(engines, resource_obj=aaa.tacacs.server.server_id['2.4.6.8'], conf={
                AaaConsts.PRIORITY: 2,
                AaaConsts.TIMEOUT: rand_timeout2,
                AaaConsts.SECRET: "xyz",
                AaaConsts.PORT: AaaConsts.AAA_SERVER_BAD_PORT
            }, apply=True, verify_apply=False)

        with allure.step('Disconnect and make authentication attempt with 2 unreachable servers'):
            engines.dut.disconnect()
            _, timestamp1 = authenticator.attempt_login_failure()
            _, timestamp2 = authenticator.attempt_login_success(restart_session_process=False)

        with allure.step('Verify respond time >= sum of timeouts'):
            assert timestamp2 - timestamp1 >= rand_timeout + rand_timeout2, \
                f'Timeout was too short. Expected: {rand_timeout + rand_timeout2}'
    finally:
        logger.info('Disconnect local engine for cleanup steps')
        engines.dut.disconnect()

        # with allure.step('Remote reboot'):
        #     NvueGeneralCli(engines.dut).remote_reboot(topology_obj)

        # with allure.step('Clear aaa configuration'):
        #     aaa.unset(apply=True).verify_result()
