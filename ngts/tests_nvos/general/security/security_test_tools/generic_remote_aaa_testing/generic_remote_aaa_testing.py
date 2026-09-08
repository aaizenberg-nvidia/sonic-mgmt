from __future__ import annotations

from typing import Callable, Any
import logging
import random
import pytest
import time

from ..constants import AddressingType, AuthConsts, AuthMedium, AaaConsts, UserRole
from ngts.nvos_constants.constants_nvos import ApiType, ConfState, TestFlowType
from ngts.nvos_tools.system.RemoteAaaResource import RemoteAaaResource
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from .constants import RemoteAaaConsts, RemoteAaaType, ValidValues
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils import nvos_general_utils
from ngts.nvos_tools.system.Server import ServerId
from ...test_aaa_ldap.constants import LdapConsts
from ngts.ngts_types import EnginesT, TopologyT
from ..tool_classes import RemoteAaaServerInfo
from ..tool_classes.UserInfo import UserInfo
from ngts.nvos_tools.system.Aaa import Aaa
from . import generic_aaa_testing_utils
from .. import security_test_utils
from .. import resource_utils

logger = logging.getLogger(__name__)


def generic_aaa_test_set_unset_show(
    test_api: str,
    engines: EnginesT,
    remote_aaa_type: str,
    main_resource_obj: RemoteAaaResource,
    confs: dict[BaseComponent, dict],
    server_conf: dict,
    default_confs: dict[BaseComponent, dict],
) -> None:
    """
    @summary: Verify set, unset, show commands for remote AAA feature

        Steps:
        1. set general/global configurations
        2. set servers
            1- with default configuration
            2- with new configuration
        3. apply changes
        4. verify new configurations with show commands
            1- general configurations as required
            2- server1 configuration as default
            3- server2 configuration as required
        5. unset configurations
        6. verify default configuration
    @param test_api: run commands with NVUE / OpenApi
    @param engines: engines object
    @param remote_aaa_type: name of he remote Aaa type (tacacs, ldap, radius)
    @param main_resource_obj: BaseComponent object representing the feature resource
    @param confs: configurations to set
    @param server_conf: configuration for server2 (the non-default one)
    @param default_confs: default configurations
    """
    assert remote_aaa_type in RemoteAaaType.ALL_TYPES, f'{remote_aaa_type} is not one of {RemoteAaaType.ALL_TYPES}'

    TestToolkit.tested_api = test_api

    def show_and_parse(resource: BaseComponent, rev: str = ''):
        if TestToolkit.tested_api == ApiType.OPENAPI:
            time.sleep(0.5)
        return OutputParsingTool.parse_json_str_to_dictionary(resource.show(rev=rev)).get_returned_value()

    with allure.step('Set general configuration'):
        for resource, conf in confs.items():
            resource_utils.configure_resource(engines, resource, conf)

    with allure.step('Set servers'):
        server1 = '1.2.3.4'
        server2 = '2.3.4.5'
        server3 = AaaConsts.VM_AAA_SERVER_DN
        server4 = AaaConsts.VM_AAA_SERVER_IPV6_ADDR
        main_resource_obj.server.set(server1)
        server_conf[AaaConsts.PRIORITY] = 2
        resource_utils.configure_resource(engines, main_resource_obj.server.server_id[server2], server_conf)
        server_conf[AaaConsts.PRIORITY] = 3
        resource_utils.configure_resource(engines, main_resource_obj.server.server_id[server3], server_conf)
        server_conf[AaaConsts.PRIORITY] = 4
        resource_utils.configure_resource(engines, main_resource_obj.server.server_id[server4], server_conf, apply=True)
        non_default_servers = [server2, server3]

    with allure.step('Verify general configurations'):
        for resource, expected_conf in confs.items():
            with allure.step(f'Verify {resource.get_resource_path()} configuration'):
                cur_conf = show_and_parse(resource, ConfState.APPLIED)
                if AaaConsts.SECRET in expected_conf.keys():
                    expected_conf[AaaConsts.SECRET] = '*'
                ValidationTool.validate_fields_values_in_output(expected_fields=expected_conf.keys(),
                                                                expected_values=expected_conf.values(),
                                                                output_dict=cur_conf).verify_result()

    with allure.step('Verify servers exist in show output'):
        show_rev_param = '' if remote_aaa_type == RemoteAaaType.LDAP else ConfState.APPLIED
        show_server_output = show_and_parse(main_resource_obj.server, rev=show_rev_param)
        ValidationTool.verify_field_exist_in_json_output(show_server_output,
                                                         [server1, server2, server3, server4]).verify_result()

    with allure.step('Verify servers configurations'):
        with allure.step(f'Verify default configuration for server {server1}'):
            global_conf = show_and_parse(main_resource_obj)
            expected_conf = {
                key: 1 if key == AaaConsts.PRIORITY else global_conf[key]
                for key in server_conf.keys()
            } if remote_aaa_type == RemoteAaaType.LDAP else {AaaConsts.PRIORITY: 1}
            cur_server_conf = show_and_parse(main_resource_obj.server.server_id[server1], rev=show_rev_param)
            ValidationTool.validate_fields_values_in_output(expected_fields=expected_conf.keys(),
                                                            expected_values=expected_conf.values(),
                                                            output_dict=cur_server_conf).verify_result()

        with allure.step(f'Verify new configuration for servers {non_default_servers}'):
            expected_conf = server_conf.copy()
            expected_conf[AaaConsts.PRIORITY] = 2
            if AaaConsts.SECRET in expected_conf.keys():
                expected_conf[AaaConsts.SECRET] = '*'
            for server in non_default_servers:
                cur_server_conf = show_and_parse(main_resource_obj.server.server_id[server], rev=show_rev_param)
                ValidationTool.validate_fields_values_in_output(expected_fields=expected_conf.keys(),
                                                                expected_values=expected_conf.values(),
                                                                output_dict=cur_server_conf).verify_result()
                expected_conf[AaaConsts.PRIORITY] += 1

    if list(server_conf.keys()) != [AaaConsts.PRIORITY]:
        with allure.step(f'Clear server {server2} configuration'):
            for field in server_conf.keys():
                if field != AaaConsts.PRIORITY:
                    main_resource_obj.server.server_id[server2].unset(field).verify_result()
            SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config, engines.dut,
                                            True).verify_result()
        with allure.step(f'Verify default configuration for server {server2}'):
            global_conf = show_and_parse(main_resource_obj, rev=show_rev_param)
            expected_conf = {
                key: 2 if key == AaaConsts.PRIORITY else global_conf[key]
                for key in server_conf.keys()
            } if remote_aaa_type == RemoteAaaType.LDAP else {AaaConsts.PRIORITY: 2}
            cur_server_conf = show_and_parse(main_resource_obj.server.server_id[server2], rev=show_rev_param)
            ValidationTool.validate_fields_values_in_output(expected_fields=expected_conf.keys(),
                                                            expected_values=expected_conf.values(),
                                                            output_dict=cur_server_conf).verify_result()

    with allure.step('Unset configuration'):
        time.sleep(0.5)
        main_resource_obj.unset(apply=True).verify_result()

    with allure.step('Verify default configuration with show command'):
        for resource, expected_conf in default_confs.items():
            with allure.step(f'Verify default configuration for {resource.get_resource_path()}'):
                cur_conf = show_and_parse(resource, rev=show_rev_param)
                if AaaConsts.SECRET in expected_conf.keys():
                    # expected_conf[AaaConsts.SECRET] = '*'
                    del expected_conf[AaaConsts.SECRET]
                ValidationTool.validate_fields_values_in_output(expected_fields=expected_conf.keys(),
                                                                expected_values=expected_conf.values(),
                                                                output_dict=cur_conf).verify_result()


def generic_aaa_test_set_invalid_param(
    test_api: str,
    field_is_numeric: dict[str, bool],
    valid_values: dict,
    resources_and_fields: dict[BaseComponent, list[str]],
) -> None:
    """
    @summary: Verify set, unset, show commands for remote AAA feature

        Flow:
        - for every given resource (related to AAA feature):
            - go over all fields, set it with invalid value, and verify failure
    @param test_api: api to use
    @param field_is_numeric: dictionary for each field, whether it is numeric or not
    @param valid_values: dictionary for each field, it's valid values
    @param resources_and_fields: dictionary containing resource object as key, and it's list of fields to set as value
    """
    TestToolkit.tested_api = test_api

    def check_invalid_set_to_resource(resource_obj, field_name):
        if TestToolkit.tested_api == ApiType.NVUE and field_name != AaaConsts.SECRET:
            logger.info(f'Set {field_name} to: nothing (incomplete)')
            resource_obj.set(field_name, '').verify_result(False)

        if valid_values[field_name] != str:
            invalid_value = RandomizationTool.get_random_string(6)
            logger.info(f'Set {field_name} to: {invalid_value}')
            resource_obj.set(field_name, invalid_value).verify_result(False)

        if field_is_numeric[field_name]:
            invalid_value = RandomizationTool.select_random_value(
                list_of_values=list(range(-1000, 1000)),
                forbidden_values=valid_values[field_name]).get_returned_value()
            logger.info(f'Set {field_name} to: {invalid_value}')
            resource_obj.set(field_name, invalid_value).verify_result(False)

        if field_name == AaaConsts.SECRET:
            logger.info(f'Set {field_name} to: empty string (\'""\')')
            resource_obj.set(field_name, '""', apply=True).verify_result(False)
            generic_aaa_testing_utils.detach_config()

    for resource, fields in resources_and_fields.items():
        for field in fields:
            with allure.step(f'Check invalid {field} for {resource.get_resource_path()}'):
                check_invalid_set_to_resource(resource, field)


def validate_params(
    test_flow: str = '',
    test_api: str = '',
    addressing_type: str = '',
    remote_aaa_type: str = '',
    auth_mediums: list[str] = None,
) -> None:
    if test_flow:
        assert test_flow in TestFlowType.ALL_TYPES, f'{test_flow} is not one of {TestFlowType.ALL_TYPES}'
    if test_api:
        assert test_api in ApiType.ALL_TYPES, f'{test_api} is not one of {ApiType.ALL_TYPES}'
    if addressing_type:
        assert addressing_type in AddressingType.ALL_TYPES, f'{addressing_type} is not one of {AddressingType.ALL_TYPES}'
    if remote_aaa_type:
        assert remote_aaa_type in RemoteAaaType.ALL_TYPES, f'{remote_aaa_type} is not one of {RemoteAaaType.ALL_TYPES}'
    if auth_mediums:
        for medium in auth_mediums:
            assert medium in AuthMedium.ALL_MEDIUMS, f'{medium} is not one of {AuthMedium.ALL_MEDIUMS}'


def verify_auth(
    test_flow: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    good_flow_users: list[UserInfo] = None,
    bad_flow_users: list[UserInfo] = None,
    verify_authorization: bool = True,
    skip_auth_mediums: list[str] = None,
):
    validate_params(test_flow=test_flow, auth_mediums=skip_auth_mediums)
    if test_flow == TestFlowType.GOOD_FLOW and good_flow_users:
        security_test_utils.verify_users_auth(engines, topology_obj, good_flow_users, [True] * len(good_flow_users), verify_authorization,
                                              skip_auth_mediums)
    elif test_flow == TestFlowType.BAD_FLOW and bad_flow_users:
        security_test_utils.verify_users_auth(engines, topology_obj, bad_flow_users, [False] * len(bad_flow_users), verify_authorization,
                                              skip_auth_mediums)


def generic_aaa_test_auth(
    test_flow: str,
    test_api: str,
    addressing_type: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    local_adminuser: UserInfo,
    request: pytest.FixtureRequest,
    remote_aaa_type: str,
    remote_aaa_obj: RemoteAaaResource,
    server_by_addr_type: dict[str, RemoteAaaServerInfo.RemoteAaaServerInfo],
    test_param: list[str] = None,
    test_param_update_func: Callable[[Any, Any, RemoteAaaServerInfo.RemoteAaaServerInfo, ServerId, str], None] = None,
    skip_auth_mediums: list[str] = None,
) -> None:
    """
    @summary: Basic test to verify authentication and authorization through remote aaa, using all possible auth mediums:
        SSH, OpenApi, rcon, scp.

        Steps:
        1. configure aaa server
        2. set authentication order, and set failthrough off
        3. verify only remote user can authenticate
            - verify auth with remote user - expect success
            - verify auth with local user - expect fail
    @param test_flow: whether it's a good-flow / bad-flow test
    @param test_api: run commands with NVUE / OpenApi
    @param addressing_type: whether to check connectivity with ipv4/ipv6/domain-name addressing
    @param engines: engines object
    @param topology_obj: topology object
    @param local_adminuser: local admin user info
    @param request: object containing pytest information about current test
    @param remote_aaa_type: name of he remote Aaa type (tacacs, ldap, radius)
    @param remote_aaa_obj: BaseComponent object representing the feature resource
    @param server_by_addr_type: dictionary containing server info, by addressing type
    @param test_param: list of other parameters to run the test on
    @param test_param_update_func: function to update the test configuration for each test param
    @param skip_auth_mediums: auth mediums to skip from the test (optional)
    """
    validate_params(test_flow=test_flow, test_api=test_api, addressing_type=addressing_type,
                    remote_aaa_type=remote_aaa_type, auth_mediums=skip_auth_mediums)

    TestToolkit.tested_api = test_api
    item = request.node

    with allure.step(f'Configure {remote_aaa_type} server'):
        server = server_by_addr_type[addressing_type].copy()
        assert getattr(server, 'users_per_auth_medium', None) is not None, (f'given server must have "users_per_auth_medium" attr\n'
                                                                            f'server: {server.hostname} - {server.port} - {server.docker_name}')

        server.verify_availability().verify_result()

        server_resource = remote_aaa_obj.server.server_id[server.hostname]
        server.configure(engines)

    with allure.step(f'Enable {remote_aaa_type}'):
        remote_aaa_obj.enable(apply=True, verify_res=False)
        RemoteAaaServerInfo.update_active_aaa_server(item, server)
        if remote_aaa_type == RemoteAaaType.LDAP:
            nvos_general_utils.wait_for_ldap_nvued_restart_workaround(item, engine_to_use=engines.dut)

    if test_param:
        assert test_param_update_func, 'test_param_update_func function was not specified!'
        with allure.step(f'test through params: {test_param}'):
            for param in test_param:
                with allure.step(param):
                    with allure.step(f'Update test param: {param}'):
                        test_param_update_func(engines, item, server, server_resource, param)
                        if remote_aaa_type == RemoteAaaType.LDAP:
                            nvos_general_utils.wait_for_ldap_nvued_restart_workaround(item, engine_to_use=engines.dut)

                    with allure.step('Test auth'):
                        security_test_utils.verify_auth_mediums(test_flow, engines, topology_obj, True, False,
                                                                server, UserRole.ALL_ROLES, [local_adminuser], skip_auth_mediums=skip_auth_mediums)
    else:
        security_test_utils.verify_auth_mediums(test_flow, engines, topology_obj, True, False,
                                                server, UserRole.ALL_ROLES, [local_adminuser], skip_auth_mediums=skip_auth_mediums)


def generic_aaa_test_bad_configured_server(
    test_api: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    remote_aaa_type: str,
    remote_aaa_obj: RemoteAaaResource,
    bad_param_name: str,
    bad_configured_server: RemoteAaaServerInfo.RemoteAaaServerInfo,
    skip_auth_mediums: list[str] = None,
) -> None:
    """
    @summary: Verify that when configuring remote AAA server with wrong required value, it is unreachable,
        and remote user can't authenticate

        Steps:
        1. configure aaa server with bad required param
        2. enable remote auth method
        3. verify remote user can't authenticate
    @param test_api: run commands with NVUE / OpenApi
    @param engines: engines object
    @param topology_obj: topology object
    @param remote_aaa_type: name of he remote Aaa type (tacacs, ldap, radius)
    @param remote_aaa_obj: BaseComponent object representing the feature resource
    @param bad_param_name: name of the field to assign the bad value to
    @param bad_configured_server: object containing the remote server info
    @param skip_auth_mediums: auth mediums to skip from the test (optional)
    """
    validate_params(remote_aaa_type=remote_aaa_type, auth_mediums=skip_auth_mediums)

    TestToolkit.tested_api = test_api

    with allure.step(f'Configure {remote_aaa_type} server with bad {bad_param_name}'):
        bad_configured_server.configure(engines)

    with allure.step(f'Enable {remote_aaa_type}'):
        remote_aaa_obj.enable(apply=True, verify_res=True)

    with allure.step(f'Verify auth with {remote_aaa_type} user. Expect fail'):
        verify_auth(TestFlowType.BAD_FLOW, engines, topology_obj,
                    bad_flow_users=[random.choice(bad_configured_server.users)], verify_authorization=False,
                    skip_auth_mediums=skip_auth_mediums)


def generic_aaa_test_unique_priority(test_api: str, remote_aaa_obj: RemoteAaaResource) -> None:
    """
    @summary: Verify that server priority must be unique

        Steps:
        1. Set 2 servers with different priority - expect success
        2. set another server with existing priority - expect failure
    @param test_api: run commands with NVUE / OpenApi
    @param remote_aaa_obj: BaseComponent object representing the feature resource
    """
    validate_params(test_api=test_api)

    TestToolkit.tested_api = test_api

    with allure.step('Set 2 servers with different priority - expect success'):
        rand_prio1 = RandomizationTool.select_random_value(ValidValues.PRIORITY).get_returned_value()
        remote_aaa_obj.server.server_id['1.2.3.4'].set(AaaConsts.PRIORITY, rand_prio1).verify_result()
        rand_prio2 = RandomizationTool.select_random_value(ValidValues.PRIORITY,
                                                           forbidden_values=[rand_prio1]).get_returned_value()
        remote_aaa_obj.server.server_id['2.4.6.8'].set(AaaConsts.PRIORITY, rand_prio2,
                                                       apply=True).verify_result()

    with allure.step('Set another server with existing priority - expect fail'):
        remote_aaa_obj.server.server_id['3.6.9.12'].set(AaaConsts.PRIORITY, rand_prio2,
                                                        apply=True).verify_result(False)


def _acting_identity_was_revoked_by_apply(set_result: ResultObj) -> bool:
    """
    @summary: Tell whether an apply failed only because it revoked the identity that performed it.

        Promoting a server makes it the authoritative one, so the remote admin of the previously top
        server is no longer authenticated (failthrough is disabled - there is no local fallback).
        NVUE keeps working over its already-authenticated SSH session, but OpenApi authenticates per
        request, so the post-apply status poll answers 401. That 401 can only be produced by the DUT
        after the new config took effect, which makes it evidence that the apply itself succeeded.

        The promotion is not taken on trust: the next loop iteration waits for readiness and then
        authenticates as the promoted server's users, so an apply that did not take effect fails
        there instead of passing silently.
    """
    info = str(set_result.info or '')
    return not set_result.result and any(
        indicator in info for indicator in RemoteAaaConsts.AUTH_REVOKED_BY_APPLY_INDICATORS)


def generic_aaa_test_priority(
    test_flow: str,
    test_api: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    request: pytest.FixtureRequest,
    remote_aaa_type: str,
    remote_aaa_obj: RemoteAaaResource,
    server1: RemoteAaaServerInfo.RemoteAaaServerInfo, server2: RemoteAaaServerInfo.RemoteAaaServerInfo,
    skip_auth_mediums: list[str] = None,
) -> None:
    """
    @summary: Verify that auth is done via the lowest prioritized server (lowest number - better in priority)

        Steps:
        1. set and prioritize 2 servers
        2. verify auth is done via top prioritized server
        3. advance the lowest prioritized server to be most prioritized
        4. repeat steps 2-3 until reach priority 8 (max)

        NOTE: in order to make this test meaningful, user should provide 2 servers info, with distinct users credentials
    @param test_flow: whether it's a good-flow / bad-flow test
    @param test_api: run commands with NVUE / OpenApi
    @param engines: engines object
    @param topology_obj: topology object
    @param request: object containing pytest information about current test
    @param remote_aaa_type: name of he remote Aaa type (tacacs, ldap, radius)
    @param remote_aaa_obj: BaseComponent object representing the feature resource
    @param server1: object containing remote server info
    @param server2: another server info (with different users credentials)
    @param skip_auth_mediums: auth mediums to skip from the test (optional)
    """
    validate_params(test_flow=test_flow, test_api=test_api, remote_aaa_type=remote_aaa_type, auth_mediums=skip_auth_mediums)

    server1.verify_availability().verify_result()
    server2.verify_availability().verify_result()

    TestToolkit.tested_api = test_api
    item = request.node

    with allure.step(f'Set and prioritize 2 {remote_aaa_type} servers'):
        server1.priority = 8
        server2.priority = 7
        server1.configure(engines, set_explicit_priority=True)
        server2.configure(engines, set_explicit_priority=True)

    with allure.step(f'Enable {remote_aaa_type}'):
        remote_aaa_obj.enable(apply=True, verify_res=False)
        best_server = server2
        worse_server = server1
        RemoteAaaServerInfo.update_active_aaa_server(item, best_server)
        if remote_aaa_type == RemoteAaaType.LDAP:
            nvos_general_utils.wait_for_ldap_nvued_restart_workaround(item, engine_to_use=engines.dut)

    while True:
        with allure.step('Wait for configuration to be fully applied'):
            nvos_general_utils.wait_for_ldap_nvued_restart_workaround(
                test_item=item,
                engine_to_use=engines.dut,
                username=best_server.users[0].username,
            )

        with allure.step(f'Verify auth is done via top prioritized server: {best_server.hostname}'):
            verify_auth(test_flow, engines, topology_obj,
                        good_flow_users=[best_server.users[0]], bad_flow_users=[worse_server.users[0]],
                        verify_authorization=False, skip_auth_mediums=skip_auth_mediums)

        if best_server.priority == ValidValues.PRIORITY[0]:
            break

        next_prio = random.randint(ValidValues.PRIORITY[0], best_server.priority - 1)
        with allure.step(f'Advance lower server to be top prioritized to: {next_prio}'):
            worse_server_resource = remote_aaa_obj.server.server_id[worse_server.hostname]
            worse_server.priority = next_prio
            if remote_aaa_type == RemoteAaaType.LDAP:
                # Promote ignore_result -> verify_result: if NVUE silently
                # fails to queue the port change, the subsequent auth would
                # fail with a confusing SSH/LDAP error several seconds later.
                # Catch the real error at its source instead.
                remote_aaa_obj.set(
                    LdapConsts.PORT, worse_server.port,
                    dut_engine=item.active_remote_admin_engine,
                ).verify_result()
            set_result = worse_server_resource.set(
                AaaConsts.PRIORITY, worse_server.priority, apply=True,
                dut_engine=item.active_remote_admin_engine,
            )
            identity_revoked = _acting_identity_was_revoked_by_apply(set_result)
            if set_result.result or identity_revoked:
                # Rotate the acting identity only once the apply is known to have taken effect:
                # authority moved to the promoted server, so further requests (and the teardown)
                # must use its remote admin. On a real failure the old admin is still the valid one.
                worse_server, best_server = best_server, worse_server
                RemoteAaaServerInfo.update_active_aaa_server(item, best_server)
            if identity_revoked:
                # The result must be consumed, not just logged: the autouse `verify_result_objects`
                # fixture fails the test at teardown for any ResultObj left holding a failed result.
                # Reported as a step so the report shows the 401 was accepted deliberately here.
                with allure.step(f'Apply revoked the acting remote admin, as expected when promoting '
                                 f'"{best_server.hostname}" - the config took effect'):
                    set_result.verify_result(should_succeed=False)
            else:
                set_result.verify_result()
            if remote_aaa_type == RemoteAaaType.LDAP:
                nvos_general_utils.wait_for_ldap_nvued_restart_workaround(item, engine_to_use=engines.dut)


def generic_aaa_test_server_unreachable(
    test_flow: str,
    test_api: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    request: pytest.FixtureRequest,
    local_adminuser: UserInfo,
    remote_aaa_type: str,
    remote_aaa_obj: RemoteAaaResource,
    server1: RemoteAaaServerInfo.RemoteAaaServerInfo,
    server2: RemoteAaaServerInfo.RemoteAaaServerInfo,
    skip_auth_mediums: list[str] = None,
) -> None:
    """
    @summary: Verify that when a server is unreachable, auth is done via next in line
        (next server or next authentication method – local)

        Steps:
        1.	Configure aaa method
        2.	Enable aaa method
        3.	Make server unreachable
        4.	Verify auth - success only with local user
        5.	Configure secondary prioritized server
        6.	Verify auth – success only with 2nd server user
        7.	Make the 2nd server also unreachable
        8.	Verify auth – success only with local user
        9.	Bring back the first server
        10. Verify auth – success only with top server user
    @param test_flow: whether it's a good-flow / bad-flow test
    @param test_api: run commands with NVUE / OpenApi
    @param engines: engines object
    @param topology_obj: topology object
    @param request: object containing pytest information about current test
    @param remote_aaa_type: name of he remote Aaa type (tacacs, ldap, radius)
    @param local_adminuser: info of local admin user
    @param remote_aaa_obj: BaseComponent object representing the feature resource
    @param server1: object containing remote server info
    @param server2: another server info (with different users credentials)
    @param skip_auth_mediums: auth mediums to skip from the test (optional)
    """
    validate_params(test_api=test_api, remote_aaa_type=remote_aaa_type)

    TestToolkit.tested_api = test_api
    item = request.node

    with allure.step(f'Pre-test cleanup: remove any pre-existing {remote_aaa_type} config'):
        # Guarantees a clean slate for the priority-1 server this test is about
        # to configure. Without this, any leftover server with priority=1 on
        # the DUT (from a prior interrupted test session) makes the enable+apply
        # in this test fail with "priority must be unique" before a single auth
        # attempt is made, masking the actual scenario under test.
        try:
            remote_aaa_obj.unset(apply=True)
        except Exception as ex:
            logging.info(
                f'Pre-test {remote_aaa_type} unset produced no-op or error '
                f'(continuing - cleanup is best-effort): {ex}'
            )

    with allure.step('Configure unreachable server'):
        server1 = server1.copy()
        server2 = server2.copy()
        server1.priority = 2
        server2.priority = 1
        best_server = server2
        worse_server = server1
        best_server.configure(engines, set_explicit_priority=True)
        best_server.make_unreachable(engines)

    with allure.step(f'Enable {remote_aaa_type}'):
        remote_aaa_obj.enable(apply=True)
        if remote_aaa_type == RemoteAaaType.LDAP:
            nvos_general_utils.wait_for_ldap_nvued_restart_workaround(item, engine_to_use=engines.dut)

    with allure.step('Verify auth - success only with local user'):
        verify_auth(test_flow, engines, topology_obj,
                    good_flow_users=[local_adminuser], bad_flow_users=[random.choice(best_server.users)],
                    verify_authorization=False, skip_auth_mediums=skip_auth_mediums)

    with allure.step('Configure worse prioritized reachable server'):
        worse_server.configure(engines, set_explicit_priority=True, apply=True)
        RemoteAaaServerInfo.update_active_aaa_server(item, worse_server)
        if remote_aaa_type == RemoteAaaType.LDAP:
            nvos_general_utils.wait_for_ldap_nvued_restart_workaround(item, engine_to_use=engines.dut)

    with allure.step('Verify auth – success only with worse server user'):
        verify_auth(test_flow, engines, topology_obj,
                    good_flow_users=[random.choice(worse_server.users)], bad_flow_users=[local_adminuser],
                    verify_authorization=False, skip_auth_mediums=skip_auth_mediums)

    with allure.step('Make the worse server also unreachable'):
        worse_server.make_unreachable(engines, apply=True, dut_engine=item.active_remote_admin_engine)
        RemoteAaaServerInfo.update_active_aaa_server(item, None)
        if remote_aaa_type == RemoteAaaType.LDAP:
            nvos_general_utils.wait_for_ldap_nvued_restart_workaround(item, engine_to_use=engines.dut)

    with allure.step('Verify auth - success only with local user'):
        verify_auth(test_flow, engines, topology_obj,
                    good_flow_users=[local_adminuser], bad_flow_users=[random.choice(worse_server.users)],
                    verify_authorization=False, skip_auth_mediums=skip_auth_mediums)

    with allure.step('Bring back the best server'):
        best_server.make_reachable(engines, apply=True)
        RemoteAaaServerInfo.update_active_aaa_server(item, best_server)
        if remote_aaa_type == RemoteAaaType.LDAP:
            nvos_general_utils.wait_for_ldap_nvued_restart_workaround(item, engine_to_use=engines.dut)

    with allure.step('Verify auth – success only with best server user'):
        verify_auth(test_flow, engines, topology_obj,
                    good_flow_users=[best_server.users[0]], bad_flow_users=[local_adminuser, worse_server.users[0]],
                    verify_authorization=False, skip_auth_mediums=skip_auth_mediums)


def generic_aaa_test_auth_error(
    test_flow: str,
    test_api: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    request: pytest.FixtureRequest,
    local_adminuser: UserInfo,
    remote_aaa_type: str,
    remote_aaa_obj: RemoteAaaResource,
    server1: RemoteAaaServerInfo.RemoteAaaServerInfo,
    server2: RemoteAaaServerInfo.RemoteAaaServerInfo,
    skip_auth_mediums: list[str] = None,
) -> None:
    """
    @summary: Verify the behavior in case of auth error (username not found or bad credentials).

        In case of auth error (username not found, or bad credentials):
        - if failthrough is off -> fail authentication attempt
        - if failthrough is on  -> check credentials on the next server/auth method.

        Steps:
        1.	Configure remote aaa servers
        2.	Set failthrough off
        3.	Verify auth with 2nd server credentials – expect fail
        4.  Verify auth with local user credentials - expect fail
        5.	Set failthrough on
        6.	Verify auth with 2nd server credentials – expect success
        7.  Verify auth with local user credentials - expect success
    @param test_flow: whether it's a good-flow / bad-flow test
    @param test_api: run commands with NVUE / OpenApi
    @param engines: engines object
    @param topology_obj: topology object
    @param request: object containing pytest information about current test
    @param remote_aaa_type: name of he remote Aaa type (tacacs, ldap, radius)
    @param local_adminuser: info of local admin user
    @param remote_aaa_obj: BaseComponent object representing the feature resource
    @param server1: object containing remote server info
    @param server2: another server info (with different users credentials)
    @param skip_auth_mediums: auth mediums to skip from the test (optional)
    """
    validate_params(test_flow=test_flow, test_api=test_api, remote_aaa_type=remote_aaa_type,
                    auth_mediums=skip_auth_mediums)

    server1.verify_availability().verify_result()
    server2.verify_availability().verify_result()

    TestToolkit.tested_api = test_api
    item = request.node

    with allure.step(f'Configure {remote_aaa_type} servers'):
        server1 = server1.copy()
        server2 = server2.copy()
        server1.priority = 2
        server2.priority = 1
        best_server = server2
        worse_server = server1
        best_server.configure(engines, set_explicit_priority=True)
        worse_server.configure(engines, set_explicit_priority=True)

    with allure.step(f'Enable {remote_aaa_type} and disable failthrough'):
        remote_aaa_obj.enable(apply=True, verify_res=False)
        RemoteAaaServerInfo.update_active_aaa_server(item, best_server)
        if remote_aaa_type == RemoteAaaType.LDAP:
            nvos_general_utils.wait_for_ldap_nvued_restart_workaround(item, engine_to_use=engines.dut)

    with allure.step('Verify auth fail with users not from best server'):
        verify_auth(test_flow, engines, topology_obj, bad_flow_users=[worse_server.users[0], local_adminuser],
                    verify_authorization=False, skip_auth_mediums=skip_auth_mediums)

    with allure.step('Enable failthrough'):
        aaa: Aaa = remote_aaa_obj.parent_obj
        aaa.authentication.set(AuthConsts.FAILTHROUGH, AaaConsts.ENABLED, apply=True,
                               dut_engine=item.active_remote_admin_engine).verify_result()
        RemoteAaaServerInfo.update_active_aaa_server(item, None)
        if remote_aaa_type == RemoteAaaType.LDAP:
            nvos_general_utils.wait_for_ldap_nvued_restart_workaround(item, engine_to_use=engines.dut)

    good_flow_users = [local_adminuser]
    if remote_aaa_type != RemoteAaaType.LDAP:  # with LDAP + failthrough on - only move to next method, and not server
        good_flow_users.append(worse_server.users[0])

    with allure.step('Verify auth success with users not from top server'):
        verify_auth(test_flow, engines, topology_obj, good_flow_users=good_flow_users,
                    verify_authorization=False, skip_auth_mediums=skip_auth_mediums)
