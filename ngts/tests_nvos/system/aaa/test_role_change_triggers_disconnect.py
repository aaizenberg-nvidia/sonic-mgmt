import json
import crypt
import random
import yaml
import requests
import time
import logging
import pytest
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_constants.constants_nvos import SystemConsts, ApiType, CumulusConsts, RbacConsts
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.aaa.helpers import create_new_user, change_user_role
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from devts.infra.tools.connection_tools.utils import generate_strong_password
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.SerialConsoleTool import SerialConsoleTool
from ngts.nvos_tools.infra.FilesTool import FilesTool


cumulus_owner = "gpazbenitzha"


logger = logging.getLogger(__name__)

"""
These testcases cover the following feature :
CL5.13 | #4197158: Security | DoD | CAT-2 | Require users to re-authenticate when changing roles | Verification Test Plan
https://confluence.nvidia.com/pages/viewpage.action?pageId=3620326744
"""

CUSTOM_ROLE_1 = {'name': 'custom_role1', 'class_name': 'class1',
                 'paths': [{'path': '/interface', 'permission': RbacConsts.ALL},
                           {'path': '/interface/*/acl', 'permission': RbacConsts.READ_ONLY}]}

CUSTOM_ROLE_2 = {'name': 'custom_role2', 'class_name': 'class2',
                 'paths': [{'path': '/interface', 'permission': RbacConsts.ALL},
                           {'path': '/interface/*/ptp', 'permission': RbacConsts.READ_ONLY}]}

TEST_4_RECOVER_SCRIPT_NAME = 'recover_system_admin'
TEST_4_WAIT_SECONDS_FOR_DISCONNECTION_UPON_ROLE_CHANGE = 20
TEST_5_SCALE_SSH_CONNECTIONS_AMOUNT = 5
TEST_6_SCALE_SSH_USERS_AMOUNT = 3
TEST_6_SCALE_SSH_USERS_CONNECTIONS_AMOUNT = 3

TEST_8_REST_API_MIME_HEADER = {"Content-Type": "application/json"}
TEST_8_WAIT_FOR_REVISION_APPLIED_VERIFICATION_ATTEMPTS = 10
TEST_8_WAIT_FOR_REVISION_APPLIED_VERIFICATION_INTERVAL = 3

TEST_9_SSH_CONNECTIONS_AMOUNT = 3
# As per https://spiffe.io/docs/latest/spiffe-about/spiffe-concepts/#spiffe-id, this ID is valid :
# But there's probably a bug in openapi schema definition, that does not allow the use of '/' in the path. This is a workaround for now:
# TEST_9_SPIFFE_ID = 'spiffe://acme.com/billing/payments'
TEST_9_SPIFFE_ID = 'spiffe://acme.com/billing'


@pytest.fixture(scope='module', autouse=True)
def prepare_custom_roles(engines, devices):
    """
    Create custom RBAC classes, roles and users for all tests in this module
    """
    system = System()

    with allure.step('Create custom RBAC classes and roles for all tests in this module'):
        for role_description in [CUSTOM_ROLE_1, CUSTOM_ROLE_2]:
            for paths in role_description['paths']:
                system.aaa.class_rbac.set_new_class(classname=role_description['class_name'], action=RbacConsts.ALLOW, command_path=paths['path'], permission=paths['permission'], apply=False)
            system.aaa.role.set_new_role(rolename=role_description['name'], rbac_class=role_description['class_name'], apply=False)
        NvueGeneralCli.apply_config(engines.dut)

    with allure.step("Bypass entering password on sudo commands"):
        devices.dut.bypass_password_on_sudo_commands(engines.dut)

    yield

    with allure.step('Cleanup custom RBAC classes and roles for all tests in this module'):
        for role_description in [CUSTOM_ROLE_1, CUSTOM_ROLE_2]:
            system.aaa.role.role_id[role_description['name']].unset(apply=False)
            system.aaa.class_rbac.unset(op_param=role_description['class_name'], apply=False)
        NvueGeneralCli.apply_config(engines.dut)


def disconnection_message(username: str) -> str:
    return f"Session disconnected by NVUE as 'nv action system aaa' on user {username} called"


def disconnection_message_arrived_in_ssh_connection(user_connection: LinuxSshEngine, username: str):
    last_output = user_connection.engine.read_channel()
    logger.debug(f"Last output: {last_output}")
    if not last_output:
        logger.warning("Client received no notification in the closed SSH session")
    return disconnection_message(username) in last_output


def check_user_connection_closed_on_client_side(user_connection: LinuxSshEngine, username: str):
    assert user_connection.is_closed, f"User {username} SSH session should have been closed"


def check_username_is_shown_in_who_command_output(engines, username: str, times_expected: int = 1):
    username_count = __check_user_connections_status_on_server_side(engines, username)
    assert username_count == times_expected, \
        f"User {username} should be shown in who command output {times_expected} times, but is shown {username_count} times"


def check_user_is_not_connected_on_server_side(engines, username: str):
    username_count = __check_user_connections_status_on_server_side(engines, username)
    assert username_count == 0, f"User {username} should not be shown in who command output"


def __check_user_connections_status_on_server_side(engines, username: str) -> int:
    who_output = engines.dut.run_cmd('who -u')
    return who_output.count(username)


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.system
def test_default_role_switch(engines, random_api):
    """
    Test flow:
        1. Create a new user A with a default role : system-admin
        2. Establish SSH connection with user A
        3. Change user A's role to a default role : nvue-admin
        4. Verify user A's SSH connection is disconnected
        5. Verify user A is not shown in 'who' command output
    """

    with allure.step('Create a new user with a default role : system-admin'):
        username, password = create_new_user(role=CumulusConsts.ROLE_SYSTEM_ADMIN, apply=True)

    with allure.step('Establish SSH connection with user A'):
        user_connection = ConnectionTool.create_ssh_conn(ip=engines.dut.ip, username=username, password=password).get_returned_value()

    with allure.step("Change user A's role to a default role : nvue-admin"):
        change_user_role(username=username, role=CumulusConsts.ROLE_NVUE_ADMIN)

    with allure.step(f'Verify {username} client is aware the SSH session is closed from server side'):
        check_user_connection_closed_on_client_side(user_connection=user_connection, username=username)

    with allure.step(f'Verify {username} client received a disconnection message in the closed SSH session'):
        if not disconnection_message_arrived_in_ssh_connection(user_connection=user_connection, username=username):
            logger.warning("Client received no disconnection message in the closed SSH session")

    with allure.step(f'Verify {username} is not shown in "who" command output'):
        check_user_is_not_connected_on_server_side(engines=engines, username=username)


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.system
def test_custom_to_default_role_switch(engines, random_api):
    """
    Test flow:
        1. Create new user A with a custom role : custom_role1
        2. Establish SSH connection with user A
        3. Change user A's role to a default role : nvue-admin
        4. Verify user A's SSH connection is disconnected
        5. Verify user A is not shown in 'who' command output
    """

    with allure.step("Create a new user with a custom role : custom_role1"):
        username, password = create_new_user(role=CUSTOM_ROLE_1['name'], apply=True)

    with allure.step('Establish SSH connection with user A'):
        user_connection = ConnectionTool.create_ssh_conn(ip=engines.dut.ip, username=username, password=password).get_returned_value()

    with allure.step("Change user A's role to a default role : nvue-admin"):
        change_user_role(username=username, role=CumulusConsts.ROLE_NVUE_ADMIN)

    with allure.step(f'Verify {username} client is aware the SSH session is closed from server side'):
        check_user_connection_closed_on_client_side(user_connection=user_connection, username=username)

    with allure.step(f'Verify {username} client received a disconnection message in the closed SSH session'):
        if not disconnection_message_arrived_in_ssh_connection(user_connection=user_connection, username=username):
            logger.warning("Client received no disconnection message in the closed SSH session")

    with allure.step(f'Verify {username} is not shown in "who" command output'):
        check_user_is_not_connected_on_server_side(engines=engines, username=username)


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.system
def test_custom_role_switch(engines, random_api):
    """
    Test flow:
        1. Establish SSH connection with a custom user A with custom role
        2. Change user A's role to a custom role : custom_role2
        3. Verify user A's SSH connection is disconnected
        4. Verify user A is not shown in 'who' command output
    """

    with allure.step("Create a new user with a custom role : custom_role1"):
        username, password = create_new_user(role=CUSTOM_ROLE_1['name'], apply=True)

    with allure.step('Establish SSH connection with user A'):
        user_connection = ConnectionTool.create_ssh_conn(ip=engines.dut.ip, username=username, password=password).get_returned_value()

    with allure.step("Change user A's role to a custom role : custom_role2"):
        change_user_role(username=username, role=CUSTOM_ROLE_2['name'])

    with allure.step(f'Verify {username} client is aware the SSH session is closed from server side'):
        check_user_connection_closed_on_client_side(user_connection=user_connection, username=username)

    with allure.step(f'Verify {username} client received a disconnection message in the closed SSH session'):
        if not disconnection_message_arrived_in_ssh_connection(user_connection=user_connection, username=username):
            logger.warning("Client received no disconnection message in the closed SSH session")

    with allure.step(f'Verify {username} is not shown in "who" command output'):
        check_user_is_not_connected_on_server_side(engines=engines, username=username)


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.system
def test_scale_ssh(engines, random_api):
    """
    Test flow:
        1. Create new user A with a default role : system-admin
        2. Establish multiple SSH connections with user A
        3. Change user A's role to a default role : nvue-admin
        4. Verify all the user A's SSH connections are disconnected
        5. Verify user A is not shown in 'who' command output
    """

    with allure.step('Create a new user with a default role : system-admin'):
        username, password = create_new_user(role=CumulusConsts.ROLE_SYSTEM_ADMIN, apply=True)

    with allure.step(f'Establish {TEST_5_SCALE_SSH_CONNECTIONS_AMOUNT} SSH connections with user A'):
        user_connections = [result_object.get_returned_value() for result_object in
                            ConnectionTool.create_multiple_ssh_conns(ip=engines.dut.ip, username=username, password=password, amount=TEST_5_SCALE_SSH_CONNECTIONS_AMOUNT)]

    with allure.step("Change user A's role to a default role : nvue-admin"):
        change_user_role(username=username, role=CumulusConsts.ROLE_NVUE_ADMIN)

    with allure.step(f'Verify {username} client is aware all SSH connections are closed from server side'):
        for user_connection in user_connections:
            check_user_connection_closed_on_client_side(user_connection=user_connection, username=username)

    with allure.step(f'Verify {username} client received a disconnection message in at least one of the SSH connections'):
        if not any([disconnection_message_arrived_in_ssh_connection(user_connection=user_connection, username=username) for user_connection in user_connections]):
            logger.warning("Client did not get a disconnection message in any of the SSH connections")

    with allure.step(f'Verify {username} is not shown in "who" command output'):
        check_user_is_not_connected_on_server_side(engines=engines, username=username)


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.system
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_scale_ssh_users(engines, test_api):
    """
    Test flow:
        1. Create 5 users with a default role : system-admin
        2. Establish 16 SSH connections with each user
        3. Change all users' roles to a default role : nvue-admin
        4. Verify all 80 user A's SSH connections are disconnected
        5. Verify user A is not shown in 'who' command output
    """
    general_cli = TestToolkit.GeneralApi[test_api]

    with allure.step(f'Create {TEST_6_SCALE_SSH_USERS_AMOUNT} users with a default role : system-admin'):
        users_credentials = []
        for i in range(TEST_6_SCALE_SSH_USERS_AMOUNT):
            username, password = create_new_user(role=CumulusConsts.ROLE_SYSTEM_ADMIN, apply=False)
            users_credentials.append((username, password))
    NvueGeneralCli.apply_config(engines.dut)

    with allure.step(f'Establish {TEST_6_SCALE_SSH_USERS_CONNECTIONS_AMOUNT} SSH connections with each user'):
        all_users_connections = dict()  # username -> list of connections
        for username, password in users_credentials:
            user_connections = [result_object.get_returned_value() for result_object in
                                ConnectionTool.create_multiple_ssh_conns(ip=engines.dut.ip, username=username, password=password, amount=TEST_6_SCALE_SSH_USERS_CONNECTIONS_AMOUNT)]
            all_users_connections[username] = user_connections

    with allure.step("Change all users' roles to a default role : nvue-admin"):
        for username, password in users_credentials:
            change_user_role(username=username, role=CumulusConsts.ROLE_NVUE_ADMIN, apply=False)

        if any([conn.is_closed for user_conns_list in all_users_connections.values() for conn in user_conns_list]):
            pytest.fail("Some SSH connections are closed before the role change! This should not happen")

    with allure.step('Apply the role change for all users'):
        general_cli.apply_config(engines.dut)

    with allure.step(f'Verify all client connections are closed on client side'):
        for username, user_connections in all_users_connections.items():
            for conn in user_connections:
                check_user_connection_closed_on_client_side(user_connection=conn, username=username)
            if not any([disconnection_message_arrived_in_ssh_connection(user_connection=conn, username=username) for conn in user_connections]):
                logger.warning(f"User {username} did not get a disconnection message in any of the SSH connections")

    with allure.step(f"Verify all users' connections are closed on server side"):
        for username in all_users_connections.keys():
            check_user_is_not_connected_on_server_side(engines=engines, username=username)


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.system
@pytest.mark.parametrize('test_api', [ApiType.NVUE])    # OpenAPI type does not support config replacement yet
def test_config_replace_method_1(engines, devices, test_api):
    """
    Test flow:
        1. Create a new user with a default role : system-admin
        2. Generate a file with the changes and recovered passwords hashes
        3. Establish SSH connection with user A
        4. Replace config with the generated file
        5. Apply the replaced config with the role change
        6. Verify user A's SSH connection is disconnected
    """
    TestToolkit.tested_api = test_api
    general_cli = TestToolkit.GeneralApi[test_api]

    with allure.step('Create a new user with a default role : system-admin'):
        username, password = create_new_user(role=CumulusConsts.ROLE_SYSTEM_ADMIN, apply=True)

    with allure.step("Generate a file with the changes"):
        change_user_role(username=username, role=CumulusConsts.ROLE_NVUE_MONITOR, apply=False)
        config_after_role_change = general_cli.show_config(engines.dut, revision='pending', output_type='json')
        general_cli.detach_config(engines.dut)

        # Parse JSON string and modify password
        # "Show config" returns a JSON payload containing the current configuration, but
        # the passwords are obfuscated with asterisks. To preserve the current passwords,
        # here we replace them with the actual passwords' hashes.
        config_array = json.loads(config_after_role_change)
        if len(config_array) < 1:
            pytest.fail("Invalid configuration structure")
        config_main_dict = config_array[1]

        for _user, _pass in [(devices.dut.default_username, devices.dut.default_password), (username, password)]:
            # Generate a Linux-compatible hashed password
            logger.debug(f"generating hashed password for {_user} : {_pass}")
            salt = crypt.mksalt(crypt.METHOD_SHA512)
            hashed_password = crypt.crypt(_pass, salt)
            config_main_dict['set']['system']['aaa']['user'][_user]['hashed-password'] = hashed_password

        config_after_role_change = json.dumps(config_array)

        file_name = FilesTool.create_file_with_content(engines.dut, 'config_after_role_change', 'json', config_after_role_change)

    with allure.step('Establish SSH connection with user A'):
        user_connection = ConnectionTool.create_ssh_conn(ip=engines.dut.ip, username=username, password=password).get_returned_value()

    with allure.step('Replace config with the generated file'):
        general_cli.replace_config(engines.dut, file_name)
        current_diff = general_cli.diff_config(engines.dut)
        if not current_diff:
            pytest.fail("Failed to replace config")

    with allure.step('Apply the replaced config with the role change'):
        general_cli.apply_config(engines.dut)

    with allure.step(f'Verify {username} client is aware the SSH session is closed from server side'):
        check_user_connection_closed_on_client_side(user_connection=user_connection, username=username)

    with allure.step(f'Verify {username} client received a disconnection message in the closed SSH session'):
        if not disconnection_message_arrived_in_ssh_connection(user_connection=user_connection, username=username):
            logger.warning("Client received no disconnection message in the closed SSH session")

    with allure.step(f'Verify {username} is not shown in "who" command output'):
        check_user_is_not_connected_on_server_side(engines=engines, username=username)


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.system
@pytest.mark.parametrize('test_api', [ApiType.NVUE])    # No reason to test file replacement with OpenAPI type, thus only NVUE is tested
def test_config_replace_method_2(engines, devices, test_api):
    """
    Test flow:
        1. Create a new user with a default role : system-admin
        2. Copy the existing startup.yaml and modify the user's role
        3. Establish SSH connection with user A
        4. Replace startup config with the modified file
        5. Apply the startup config with the role change
        6. Verify user A's SSH connection is disconnected
    """
    TestToolkit.tested_api = test_api
    general_cli = TestToolkit.GeneralApi[test_api]

    with allure.step('Create a new user with a default role : system-admin'):
        username, password = create_new_user(role=CumulusConsts.ROLE_SYSTEM_ADMIN, apply=True)

    with allure.step("Copy and modify the startup.yaml file"):
        # Save applied config to startup.yaml
        general_cli.save_config(engines.dut)

        current_json_config = general_cli.show_config(engines.dut, revision='startup', output_type='json')
        config_array = json.loads(current_json_config)

        # Update the user's role
        if len(config_array) < 1:
            pytest.fail("Invalid startup configuration structure")
        config_main_dict = config_array[1]

        for _user, _pass in [(devices.dut.default_username, devices.dut.default_password), (username, password)]:
            # Generate a Linux-compatible hashed password
            logger.debug(f"generating hashed password for {_user} : {_pass}")
            salt = crypt.mksalt(crypt.METHOD_SHA512)
            hashed_password = crypt.crypt(_pass, salt)
            config_main_dict['set']['system']['aaa']['user'][_user]['hashed-password'] = hashed_password

        config_main_dict['set']['system']['aaa']['user'][username]['role'] = CumulusConsts.ROLE_NVUE_MONITOR

        # Write the modified configuration in yaml format back to the temporary file
        modified_config = yaml.dump(config_array)
        file_name = FilesTool.create_file_with_content(engines.dut, 'startup_config_after_role_change', 'yaml', modified_config)

    with allure.step('Establish SSH connection with user A'):
        user_connection = ConnectionTool.create_ssh_conn(ip=engines.dut.ip, username=username, password=password).get_returned_value()

    with allure.step('Replace startup config with the modified file'):
        # Copy the modified file to the correct location
        engines.dut.run_cmd(f'sudo cp -v {file_name} /etc/nvue.d/startup.yaml')

    with allure.step('Apply the startup config with the role change'):
        engines.dut.run_cmd('nv config apply startup -y')

    with allure.step(f'Verify {username} client is aware the SSH session is closed from server side'):
        check_user_connection_closed_on_client_side(user_connection=user_connection, username=username)

    with allure.step(f'Verify {username} client received a disconnection message in the closed SSH session'):
        if not disconnection_message_arrived_in_ssh_connection(user_connection=user_connection, username=username):
            logger.warning("Client received no disconnection message in the closed SSH session")

    with allure.step(f'Verify {username} is not shown in "who" command output'):
        check_user_is_not_connected_on_server_side(engines=engines, username=username)


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.system
@pytest.mark.parametrize('test_api', [ApiType.NVUE])    # This test sets a password, which is not yet supported by OpenAPI type
def test_negative_change_user_attributes(engines, test_api):
    """
    Test flow:
        1. Create a new user with a default role : system-admin
        2. Establish SSH connection with user A
        3. Change the user's full-name
        4. Change the user's password
        5. Verify the user can connect with the new password
        6. Change the user's hashed-password
        7. Verify the user can connect with the new password
        8. Change the user's SPIFFE ID
        9. Change the user's state
        10. Verify user A's original SSH connections are NOT disconnected
        11. Verify user A is shown in 'who' command output
    """
    TestToolkit.tested_api = test_api

    system = System()
    with allure.step('Create a new user with a default role : system-admin'):
        username, password = create_new_user(role=CumulusConsts.ROLE_SYSTEM_ADMIN, apply=True)

    with allure.step(f'Establish {TEST_9_SSH_CONNECTIONS_AMOUNT} SSH connections with user A'):
        user_connections = [result_object.get_returned_value() for result_object in
                            ConnectionTool.create_multiple_ssh_conns(ip=engines.dut.ip, username=username, password=password, amount=TEST_9_SSH_CONNECTIONS_AMOUNT)]

    with allure.step('Change the user\'s full-name'):
        system.aaa.user.user_id[username].set(SystemConsts.USER_FULL_NAME, '\"John Doe\"', apply=False).verify_result()

    with allure.step('Change the user\'s password'):
        new_password = generate_strong_password()
        system.aaa.user.user_id[username].set(SystemConsts.USER_PASSWORD, new_password, apply=True).verify_result()

    with allure.step(f"Verify {username} authenticates with the new password"):
        new_connection = ConnectionTool.create_ssh_conn(ip=engines.dut.ip, username=username, password=new_password).get_returned_value()
        assert new_connection, f"Failed to establish SSH connection with {username} after password change"
        new_connection.disconnect()

    with allure.step('Change the user\'s hashed-password'):
        new_password_to_hash = generate_strong_password()
        logger.debug(f"generating hashed password for {username} : {new_password_to_hash}")
        salt = crypt.mksalt(crypt.METHOD_SHA512)
        logger.debug(f"salt is {salt}")
        hashed_password = crypt.crypt(new_password_to_hash, salt)
        logger.debug(f"hashed password is {hashed_password}")
        system.aaa.user.user_id[username].set(SystemConsts.USER_HASHED_PASSWORD, f"\'{hashed_password}\'", apply=True).verify_result()

    with allure.step(f"Verify {username} authenticates with the new hashed-password"):
        connection_with_hashed_pass_auth = ConnectionTool.create_ssh_conn(ip=engines.dut.ip, username=username, password=new_password_to_hash).get_returned_value()
        assert connection_with_hashed_pass_auth, f"Failed to establish SSH connection with {username} after hashed-password change"
        connection_with_hashed_pass_auth.disconnect()

    with allure.step('Change the user\'s SPIFFE ID'):
        system.aaa.user.user_id[username].set(SystemConsts.USER_SPIFFE_ID, TEST_9_SPIFFE_ID, apply=False).verify_result()

    with allure.step('Change the user\'s state'):
        system.aaa.user.user_id[username].set(SystemConsts.USER_STATE, 'disabled', apply=True).verify_result()

    with allure.step('Verify user A\'s SSH connection is NOT disconnected'):
        for conn in user_connections:
            assert not conn.is_closed, f"User {username} SSH connection is disconnected"

    with allure.step(f'Verify {username} is shown in "who" command output'):
        check_username_is_shown_in_who_command_output(engines=engines, username=username, times_expected=TEST_9_SSH_CONNECTIONS_AMOUNT)

    with allure.step('Cleanup'):
        for conn in user_connections:
            conn.disconnect()


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.system
def test_negative_delete_user(engines, random_api):
    """
    Test flow:
        1. Create a new user with a default role : system-admin
        2. Establish SSH connection with user A
        3. Try to delete the user and verify the action failed with an error "{username} is logged in and cannot be deleted"
        5. Verify the user's session is still active
    """

    system = System()
    with allure.step('Create a new user with a default role : system-admin'):
        username, password = create_new_user(role=CumulusConsts.ROLE_SYSTEM_ADMIN, apply=True)

    with allure.step(f'Establish SSH connection with user {username}'):
        user_connection = ConnectionTool.create_ssh_conn(ip=engines.dut.ip, username=username, password=password).get_returned_value()

    with allure.step('Try to delete the user and verify the action failed with the expected error'):
        result = system.aaa.user.user_id[username].unset(apply=True)
        with pytest.raises(AssertionError):
            result.verify_result()

        assert f'{username} is logged in and cannot be deleted.' in result.info, \
            f"Expected error message not found in {result.info}"

    with allure.step('Verify the user\'s session is still active'):
        assert not user_connection.is_closed, f"User {username} SSH connection is disconnected while should be still active"
        check_username_is_shown_in_who_command_output(engines=engines, username=username)

    with allure.step('Cleanup'):
        user_connection.disconnect()


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.system
@pytest.mark.parametrize('test_api', [ApiType.NVUE])    # This test sets a password, which is not yet supported by OpenAPI type
def test_negative_change_user_attributes_and_role(engines, test_api):
    """
    Test flow:
        1. Create two users, one with custom role 1 and another with custom role 2
        2. Establish SSH connections with both users
        3. Change the user A's role
        4. Change the user B's full-name and password
        5. Verify that the user A's SSH connections are disconnected
        6. Verify that the user B's SSH connections are not disconnected
    """
    TestToolkit.tested_api = test_api

    system = System()
    with allure.step('Create two users, one with custom role 1 and another with custom role 2'):
        username_1, password_1 = create_new_user(role=CUSTOM_ROLE_1['name'], apply=True)
        username_2, password_2 = create_new_user(role=CUSTOM_ROLE_2['name'], apply=True)

    with allure.step(f'Establish SSH connections with both users'):
        user1_connections = [result_object.get_returned_value() for result_object in
                             ConnectionTool.create_multiple_ssh_conns(ip=engines.dut.ip, username=username_1, password=password_1, amount=2)]
        user2_connections = [result_object.get_returned_value() for result_object in
                             ConnectionTool.create_multiple_ssh_conns(ip=engines.dut.ip, username=username_2, password=password_2, amount=2)]

    with allure.step(f'Change the user {username_1}\'s role'):
        system.aaa.user.user_id[username_1].set(SystemConsts.USER_ROLE, CumulusConsts.ROLE_NVUE_ADMIN, apply=False).verify_result()

    with allure.step(f'Change the user {username_2}\'s full-name and password'):
        new_password = generate_strong_password()
        system.aaa.user.user_id[username_2].set(SystemConsts.USER_FULL_NAME, '\"John Doe\"', apply=False).verify_result()
        system.aaa.user.user_id[username_2].set(SystemConsts.USER_PASSWORD, new_password, apply=True).verify_result()

    with allure.step(f'Verify that the user {username_1}\'s SSH connections are disconnected'):
        for conn in user1_connections:
            assert conn.is_closed, f"User {username_1} SSH connection is not disconnected"

    with allure.step(f'Verify that the user {username_2}\'s SSH connections are not disconnected'):
        for conn in user2_connections:
            assert not conn.is_closed, f"User {username_2} SSH connection is disconnected"

    with allure.step(f'Verify username {username_1} is not shown in "who" command output but {username_2} is'):
        check_user_is_not_connected_on_server_side(engines=engines, username=username_1)
        check_username_is_shown_in_who_command_output(engines=engines, username=username_2, times_expected=2)

    with allure.step('Cleanup'):
        for conn in user1_connections + user2_connections:
            conn.disconnect()


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.system
def test_console_up(engines, random_api):
    """
    Test flow:
        1. Create a new user with a default role : system-admin
        2. Open a serial connection and login with the user's credentials
        3. Change the user's role
        4. Verify the user is logged out of the console connection
        5. Verify the user is not shown in "who" command output
    """

    with allure.step('Create a new user with a default role : system-admin'):
        username, password = create_new_user(role=CumulusConsts.ROLE_SYSTEM_ADMIN, apply=True)

    with allure.step(f'Open a serial connection and login with {username}\'s credentials'):
        topology_obj = TestToolkit.topology_obj
        with allure.step('enter to serial context'):
            serial = SerialConsoleTool.get_serial_console_session(topology_obj)
        with allure.step('exit existing login'):
            SerialConsoleTool.exit_existing_login(serial)
        SerialConsoleTool.login_nos(serial_engine=serial, username=username, password=password, start_login_tries=10, handle_change_password_prompt=False)

        check_username_is_shown_in_who_command_output(engines=engines, username=username)

    with allure.step(f'Change the {username}\'s role'):
        change_user_role(username=username, role=CumulusConsts.ROLE_NVUE_ADMIN)

    with allure.step('Check the user is logged out of console connection'):
        serial_recent_output, _ = serial.run_cmd(cmd='\n\n', expected_value='login:')
        assert disconnection_message(username) in serial_recent_output, f"User {username} is not logged out in console"

    with allure.step(f'Verify {username} is not shown in "who" command output'):
        check_user_is_not_connected_on_server_side(engines=engines, username=username)

    with allure.step('Retry logging in in console'):
        SerialConsoleTool.login_nos(serial_engine=serial, username=username, password=password, start_login_tries=10, handle_change_password_prompt=False)

    with allure.step('Cleanup'):
        SerialConsoleTool.exit_existing_login(serial)


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.system
def test_agent_restart(engines, random_api):
    """
    Test flow:
        1. Create a new user with a default role : system-admin
        2. Establish SSH connection with user A
        3. Restart NVUED
        4. Change the user's role to a default role : nvue-admin
        5. Verify the user is logged out of the SSH connection
    """

    with allure.step('Create a new user with a default role : system-admin'):
        username, password = create_new_user(role=CumulusConsts.ROLE_SYSTEM_ADMIN, apply=True)

    with allure.step('Establish SSH connection with user A'):
        user_connection = ConnectionTool.create_ssh_conn(ip=engines.dut.ip, username=username, password=password).get_returned_value()

    with allure.step('Restart NVUED'):
        def get_pid_of_nvued():
            pid_str = engines.dut.run_cmd('sudo systemctl show nvued | grep ^MainPID')
            return int(pid_str.split('=')[1])

        pid_before_restart = get_pid_of_nvued()
        engines.dut.run_cmd('sudo systemctl restart nvued')
        pid_after_restart = get_pid_of_nvued()
        assert pid_before_restart != pid_after_restart, 'Agent was not restarted'

    with allure.step("Change user A's role to a default role : nvue-admin"):
        change_user_role(username=username, role=CumulusConsts.ROLE_NVUE_ADMIN)

    with allure.step(f'Verify {username} client is aware the SSH session is closed from server side'):
        check_user_connection_closed_on_client_side(user_connection=user_connection, username=username)

    with allure.step(f'Verify {username} client received a disconnection message in the closed SSH session'):
        if not disconnection_message_arrived_in_ssh_connection(user_connection=user_connection, username=username):
            logger.warning("Client received no disconnection message in the closed SSH session")

    with allure.step(f'Verify {username} is not shown in "who" command output'):
        check_user_is_not_connected_on_server_side(engines=engines, username=username)


def __get_pid_of_recovery_script(engines):
    pid_str = engines.dut.run_cmd(f"sudo ps aux | grep {TEST_4_RECOVER_SCRIPT_NAME} | grep -v grep | grep -v sudo  | tr -s ' ' | cut -d ' ' -f 2")
    return int(pid_str) if pid_str else None


@pytest.fixture(scope='function', autouse=False)
def the_test_requires_cumulus_user_role_recovery_to_system_admin(engines):
    username = engines.dut.username
    password = engines.dut.password

    RECOVER_FLAG_FILE_NAME = '~/escalate_my_role'
    RECOVER_SCRIPT_CONTENT = f"""
#!/bin/bash -x

USERNAME='{username}'
FLAG_FILE='/var/home/'$USERNAME'/escalate_my_role'

while true; do
    rm -rf $FLAG_FILE
    while true; do
        date
        if test -f "$FLAG_FILE"; then
            break
        fi
        sleep 1
    done
    date
    sed -i -r -e 's/(^sudo\\:.*)/\\1,'$USERNAME'/g' /etc/group
    nv set system aaa user $USERNAME role system-admin
    nv config apply -y
    rm -rf $FLAG_FILE
done
    """

    with allure.step("Start the recovery script"):
        with allure.step("Create and start the script"):
            RECOVER_SCRIPT_FULL_NAME = FilesTool.create_file_with_content(engines.dut, TEST_4_RECOVER_SCRIPT_NAME, 'sh', RECOVER_SCRIPT_CONTENT)
            engines.dut.run_cmd(f"sudo chmod +x {RECOVER_SCRIPT_FULL_NAME}")
            engines.dut.run_cmd(f"sudo nohup {RECOVER_SCRIPT_FULL_NAME} 1>>/tmp/recovery_script.log 2>>/tmp/recovery_script.log &")

        with allure.step("Verify the recovery script is running, before executing the critical part"):
            recovery_script_pid_before_starting = __get_pid_of_recovery_script(engines)
            logger.debug(f"Recovery script pid before starting: {recovery_script_pid_before_starting}")
            if not recovery_script_pid_before_starting:
                recovery_script_log = engines.dut.run_cmd(f"cat /tmp/recovery_script.log")
                logger.warning(f"Will skip the test because the recovery script is not running for some reason. The script log is: {recovery_script_log}")
                pytest.skip("The recovery script is not running, will not execute the test")
                return

    yield

    with allure.step(f"Recover the system-admin role for {username}"):
        with allure.step("Verify the user does not have sudo permissions"):
            sudo_result = engines.dut.run_cmd(f"echo {password} | sudo -S echo")
            if f"Sorry, user {username} is not allowed to execute" not in sudo_result:
                logger.warning(f"Seems like {username} is still sudo-able. Will try to make the {username} system-admin anyway.")

        with allure.step("Trigger the recovery : create the recovery flag file to trigger the recovery script to change the role back"):
            engines.dut.run_cmd(f'touch {RECOVER_FLAG_FILE_NAME}')

        with allure.step(f"The recovery script changes the role to system-admin, and the connection should be interrupted not later than in {TEST_4_WAIT_SECONDS_FOR_DISCONNECTION_UPON_ROLE_CHANGE} sec from now"):
            for i in range(TEST_4_WAIT_SECONDS_FOR_DISCONNECTION_UPON_ROLE_CHANGE):
                if engines.dut.is_closed:
                    break
                time.sleep(1)

            check_user_connection_closed_on_client_side(user_connection=engines.dut, username=username)
            engines.dut.disconnect()

        with allure.step("Verify the user has sudo permissions back"):
            sudo_result = engines.dut.run_cmd(f"echo {password} | sudo -S echo")
            logger.debug(f"sudo_result ({username} should be user now): {sudo_result}")
            if sudo_result and f"Sorry, user {username} is not allowed to execute" in sudo_result:
                pytest.fail(f"Recovery failed. {username} is still not system-admin. Consider reinstalling the DUT as it might not have any system-admin user.")

        with allure.step("Stop the recovery script"):
            recovery_script_pid_after_the_test = __get_pid_of_recovery_script(engines)
            if recovery_script_pid_after_the_test != recovery_script_pid_before_starting:
                recovery_script_log = engines.dut.run_cmd(f"cat /tmp/recovery_script.log")
                logger.warning(f"Recovery script PID changed for some reason. Before the test: {recovery_script_pid_before_starting}, after the test: {recovery_script_pid_after_the_test}. The script log is: {recovery_script_log}")

            engines.dut.run_cmd(f"sudo kill -9 {recovery_script_pid_after_the_test}")
            engines.dut.run_cmd(f"sudo rm -rf {RECOVER_SCRIPT_FULL_NAME}")


@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.checklist
@pytest.mark.system
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_remove_system_admin(engines, the_test_requires_cumulus_user_role_recovery_to_system_admin, test_api):
    """
    IMPORTANT :
    This test executes a command, that changes the only user (default 'cumulus' user) role to 'nvue-admin'. But, as it's the only system-admin user by default,
    the test verifies that a warning message is presented to the user, and, as for other tests, its SSH connections are closed.
    But the problem with this test is that after that it's impossible to revert the machine, because no system-admin users exist.

    This test utilizes a workaround for this problem:
    Before starting the test, the function's fixture creates an starts a recovery script in background under sudo permissions (with nohup). The script monitors
    a file existence (file 'escalate_my_role' in home directory of the 'cumulus' user). So when the user's role is degraded to nvue-admin, he still could
    create this file, thus triggering the running script to change his role back to system-admin (it will trigger SSH disconnection again). After that the user
    is system-admin again, and no reverting to factory-defaults is needed.

    Test flow:
        1. Verify the recovery script is running, before executing the critical part
        2. Change the user role to nvue-admin
        3. Apply the config and verify the warning is shown
        4. Wait until the connection is closed
    """
    TestToolkit.tested_api = test_api

    username = engines.dut.username
    with allure.step("Verify the recovery script is running, before executing the critical part"):
        recovery_script_pid = __get_pid_of_recovery_script(engines)
        logger.debug(f"Recover script pid: {recovery_script_pid}")
        if not recovery_script_pid:
            pytest.skip("The recovery script is not running, will not execute the test")
            return

    with allure.step("Change the user role to nvue-admin"):
        change_user_role(username=username, role=CumulusConsts.ROLE_NVUE_ADMIN, apply=False)

    with allure.step("Apply the config"):
        output = engines.dut.engine.send_command_expect(f"nv config apply", expect_string="Are you sure?", max_loops=40)
        assert "No NVUE managed user accounts have the role 'system-admin'. This could result in being locked out of the system!" in output, f"The warning is not shown"
        engines.dut.engine.write_channel(b"y\n")

    with allure.step("Wait until the connection is closed"):
        for i in range(int(TEST_4_WAIT_SECONDS_FOR_DISCONNECTION_UPON_ROLE_CHANGE / 0.2)):
            if engines.dut.is_closed:
                break
            time.sleep(0.2)

        check_user_connection_closed_on_client_side(user_connection=engines.dut, username=username)
        engines.dut.engine.read_channel()
        engines.dut.disconnect()
