import crypt
import logging
from ngts.tools.test_utils import allure_utils as allure
import pytest
import time
from retry import retry
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, RebootParams
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.tests_nvos.general.security.conftest import ssh_to_device_and_retrieve_raw_login_ssh_notification
from ngts.nvos_constants.constants_nvos import CumulusConsts
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from devts.infra.tools.connection_tools.utils import generate_strong_password
from ngts.tests_nvos.general.security.security_test_tools.switch_authenticators import SshAuthenticator
from devts.infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine
from retry.api import retry_call


from retry import retry


cumulus_owner = "yleibovichwe"


logger = logging.getLogger()


def clear_system_messages(system, engines):
    """
    Method to unset the system messages for pre-login, post-login and post-logout
    :param engines: Engines object
    :param system:  System object
    """
    with allure.step('Run unset system message and apply config'):
        system.message.unset(op_param="", apply=True, dut_engine=engines.dut).verify_result()


@pytest.fixture
def system_message_cleanup(engines):
    system = System()
    yield system
    clear_system_messages(system, engines)


@retry(Exception, tries=5, delay=2)
def verify_system_message_field_with_retry(system, field_name, expected_value):
    """
    Verify a single system message field matches expected value with retry mechanism.
    Retries up to 5 times with 2 second delay (total ~10 seconds) to handle async updates.
    :param system: System object
    :param field_name: the message field name (e.g., PRE_LOGIN_MESSAGE)
    :param expected_value: expected message value
    """
    message_output = OutputParsingTool.parse_json_str_to_dictionary(system.message.show()).get_returned_value()
    ValidationTool.verify_field_value_in_output(message_output, field_name, expected_value).verify_result()


@retry(Exception, tries=5, delay=2)
def verify_system_messages_with_retry(system, expected_pre_login, expected_post_login, expected_post_logout):
    """
    Verify all system messages match expected values with retry mechanism.
    Retries up to 5 times with 2 second delay (total ~10 seconds) to handle async updates.
    :param system: System object
    :param expected_pre_login: expected pre-login message
    :param expected_post_login: expected post-login message
    :param expected_post_logout: expected post-logout message
    """
    message_output = OutputParsingTool.parse_json_str_to_dictionary(system.message.show()).get_returned_value()
    ValidationTool.verify_field_value_in_output(message_output, SystemConsts.PRE_LOGIN_MESSAGE,
                                                expected_pre_login).verify_result()
    ValidationTool.verify_field_value_in_output(message_output, SystemConsts.POST_LOGIN_MESSAGE,
                                                expected_post_login).verify_result()
    ValidationTool.verify_field_value_in_output(message_output, SystemConsts.POST_LOGOUT_MESSAGE,
                                                expected_post_logout).verify_result()


@pytest.mark.banner
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
@pytest.mark.air
def test_show_system_message(engines, devices, random_api):
    """
    Run show/set/unset system message command and verify the required pre-login message
        Test flow:
            1. Check show system message and verify all banner messages are available
            2. Run 'nv unset system message pre-login'
            3. Run 'nv unset system message post-login'
            4. Run 'nv unset system message post-logout'
            5. Run 'nv show system message'
            6. Verify that all messages have default values
    """
    system = System()
    clear_system_messages(system, engines)

    try:

        with allure.step('Run show system message command and verify that each field has a value'):
            message_output = OutputParsingTool.parse_json_str_to_dictionary(system.message.show()).get_returned_value()
            ValidationTool.verify_field_value_exist_in_output_dict(message_output,
                                                                   SystemConsts.PRE_LOGIN_MESSAGE).verify_result()
            ValidationTool.verify_field_value_exist_in_output_dict(message_output,
                                                                   SystemConsts.POST_LOGIN_MESSAGE).verify_result()
            ValidationTool.verify_field_value_exist_in_output_dict(message_output,
                                                                   SystemConsts.POST_LOGOUT_MESSAGE).verify_result()

        with allure.step('Run unset system message pre-login command and apply config'):
            system.message.unset(op_param=SystemConsts.PRE_LOGIN_MESSAGE,
                                 apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify pre-login changed to default in show system'):
            verify_system_message_field_with_retry(system, SystemConsts.PRE_LOGIN_MESSAGE,
                                                   devices.dut.pre_login_message)

        with allure.step('Run unset system message post-login command and apply config'):
            system.message.unset(op_param=SystemConsts.POST_LOGIN_MESSAGE,
                                 apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify post-login changed to default in show system'):
            TestToolkit.tested_api = ApiType.NVUE
            verify_system_message_field_with_retry(system, SystemConsts.POST_LOGIN_MESSAGE,
                                                   devices.dut.post_login_message)
            TestToolkit.tested_api = random_api

        with allure.step('Run unset system message post-logout command and apply config'):
            system.message.unset(op_param=SystemConsts.POST_LOGOUT_MESSAGE,
                                 apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify post-logout changed to default in show system'):
            verify_system_message_field_with_retry(system, SystemConsts.POST_LOGOUT_MESSAGE,
                                                   SystemConsts.POST_LOGOUT_MESSAGE_DEFAULT_VALUE)

    finally:
        clear_system_messages(system, engines)


@pytest.mark.banner
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.air
@pytest.mark.cumulus
def test_set_system_message_pre_login(engines, devices, random_api):
    """
    Run show/set/unset system message command and verify the required pre-login message
        Test flow:
            1. Set pre-login message[run cmd + apply conf]
            2. verify pre-login changed to new message in show system
            3. Verify post-login was not affected in show system
            4. Verify post-logout was not affected in show system
            5. Verify pre-login changed to new message upon connecting via SSH
            6. Verify pre-login changed to new message upon connecting via Serial
            7. Unset pre-login message[run cmd + apply conf]
            8. verify pre-login changed to default in show system
            9. Verify pre-login changed to default upon connecting via SSH
            10. Verify pre-login changed to default upon connecting via Serial
    """
    new_pre_login_msg = "Testing PRE LOGIN MESSAGE"
    system = System()

    try:
        with allure.step('Run set system message pre-login command and apply config'):
            system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value=f'"{new_pre_login_msg}"',
                               apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify pre-login changed to new message in show system'):
            verify_system_message_field_with_retry(system, SystemConsts.PRE_LOGIN_MESSAGE, new_pre_login_msg)

        with allure.step('Verify pre-login changed to new message upon connecting via SSH'):
            output = ssh_to_device_and_retrieve_raw_login_ssh_notification(engines.dut.ip,
                                                                           username=devices.dut.default_user_name,
                                                                           password=engines.dut.password)
            pre_login_output = output.split('\n')[1].strip()
            assert new_pre_login_msg == pre_login_output, \
                "Failed to set pre-login message to {pre_login}".format(pre_login=pre_login_output)

        with allure.step('Verify post-login did not change in show system'):
            TestToolkit.tested_api = ApiType.NVUE
            verify_system_message_field_with_retry(system, SystemConsts.POST_LOGIN_MESSAGE,
                                                   devices.dut.post_login_message)
            TestToolkit.tested_api = random_api

        with allure.step('Verify post-logout did not change in show system'):
            verify_system_message_field_with_retry(system, SystemConsts.POST_LOGOUT_MESSAGE,
                                                   SystemConsts.POST_LOGOUT_MESSAGE_DEFAULT_VALUE)

        with allure.step('Run unset system message pre-login command and apply config'):
            system.message.unset(op_param=SystemConsts.PRE_LOGIN_MESSAGE,
                                 apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify pre-login changed to default in show system'):
            verify_system_message_field_with_retry(system, SystemConsts.PRE_LOGIN_MESSAGE,
                                                   devices.dut.pre_login_message)

        with allure.step('Verify pre-login changed to default upon connecting via SSH'):
            output = ssh_to_device_and_retrieve_raw_login_ssh_notification(engines.dut.ip,
                                                                           username=devices.dut.default_user_name,
                                                                           password=engines.dut.password)
            pre_login_output = output.split('\n')[1].strip()
            assert pre_login_output == devices.dut.pre_login_message.strip(), \
                "Failed to set pre-login message to {pre_login}".format(pre_login=pre_login_output)

    finally:
        clear_system_messages(system, engines)


@pytest.mark.banner
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.air
@pytest.mark.cumulus
def test_set_system_message_post_login(engines, devices, random_api):
    """
    Run show/set/unset system message command and verify the required pre-login message
        Test flow:
            1. Set post-login message[run cmd + apply conf]
            2. verify post-login changed to new message in show system
            3. Verify pre-login was not affected in show system
            4. Verify post-logout was not affected in show system
            5. Verify post-login changed to new message upon connecting via SSH
            6. Verify post-login changed to new message upon connecting via Serial
            7. Unset post-login message[run cmd + apply conf]
            8. verify post-login changed to default in show system
            9. Verify post-login changed to default upon connecting via SSH
            10. Verify pre-login changed to default upon connecting via Serial
    """
    new_post_login_msg = "Testing POST LOGIN MESSAGE"
    system = System()

    try:
        with allure.step('Run set system message post-login command and apply config'):
            system.message.set(op_param_name=SystemConsts.POST_LOGIN_MESSAGE, op_param_value=f'"{new_post_login_msg}"',
                               apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify post-login changed to new message in show system'):
            verify_system_message_field_with_retry(system, SystemConsts.POST_LOGIN_MESSAGE, new_post_login_msg)

        with allure.step('Verify post-login changed to new message upon connecting via SSH'):
            output = ssh_to_device_and_retrieve_raw_login_ssh_notification(engines.dut.ip,
                                                                           username=devices.dut.default_user_name,
                                                                           password=engines.dut.password)
            assert new_post_login_msg in output, \
                "Failed to set post-login message to {post_login}\n post_login_output={post_login_output}".format(
                    post_login=new_post_login_msg, post_login_output=output)

        with allure.step('Verify pre-login did not change in show system'):
            verify_system_message_field_with_retry(system, SystemConsts.PRE_LOGIN_MESSAGE,
                                                   devices.dut.pre_login_message)

        with allure.step('Run unset system message post-login command and apply config'):
            system.message.unset(op_param=SystemConsts.POST_LOGIN_MESSAGE,
                                 apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify post-login changed to default in show system'):
            TestToolkit.tested_api = ApiType.NVUE
            verify_system_message_field_with_retry(system, SystemConsts.POST_LOGIN_MESSAGE,
                                                   devices.dut.post_login_message)
            TestToolkit.tested_api = random_api

        # TBA : SSH test for default post-login message

    finally:
        clear_system_messages(system, engines)


@pytest.mark.banner
@pytest.mark.system
@pytest.mark.air
@pytest.mark.simx
@pytest.mark.cumulus
def test_set_system_message_post_logout(engines, devices, random_api):
    """
    Run show/set/unset system message command and verify the required post-login message
        Test flow:
            1. Set post-login message[run cmd + apply conf]
            2. verify post-logout changed to new message in show system
            3. Verify pre-login was not affected in show system
            4. Verify post-login was not affected in show system
            5. Verify post-login changed to new message upon connecting via SSH
            6. Verify post-login changed to new message upon connecting via Serial
            7. Unset post-login message[run cmd + apply conf]
            8. verify post-login changed to default in show system
            9. Verify post-login changed to default upon connecting via SSH
            10. Verify post-login changed to default upon connecting via Serial
    """
    new_post_logout_msg = "Testing POST LOGOUT MESSAGE"
    system = System()

    try:
        with allure.step('Run set system message post-logout command and apply config'):
            system.message.set(op_param_name=SystemConsts.POST_LOGOUT_MESSAGE,
                               op_param_value=f'"{new_post_logout_msg}"',
                               apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify post-logout changed to new message in show system'):
            verify_system_message_field_with_retry(system, SystemConsts.POST_LOGOUT_MESSAGE, new_post_logout_msg)

        # TBA : SSH Test

        with allure.step('Verify pre-login did not change in show system'):
            verify_system_message_field_with_retry(system, SystemConsts.PRE_LOGIN_MESSAGE,
                                                   devices.dut.pre_login_message)

        with allure.step('Verify post-login did not change in show system'):
            TestToolkit.tested_api = ApiType.NVUE
            verify_system_message_field_with_retry(system, SystemConsts.POST_LOGIN_MESSAGE,
                                                   devices.dut.post_login_message)
            TestToolkit.tested_api = random_api

        with allure.step('Run unset system message post-logout command and apply config'):
            system.message.unset(op_param=SystemConsts.POST_LOGOUT_MESSAGE,
                                 apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify post-logout changed to default in show system'):
            verify_system_message_field_with_retry(system, SystemConsts.POST_LOGOUT_MESSAGE,
                                                   SystemConsts.POST_LOGOUT_MESSAGE_DEFAULT_VALUE)

        # TBA : SSH Test

    finally:
        clear_system_messages(system, engines)


@pytest.mark.banner
@pytest.mark.system
@pytest.mark.simx
def test_unset_system_message(engines, devices, random_api):
    """
    Run factory reset system message command and verify the system messages are changed to default
        Test flow:
            1. Run 'nv set system message pre-login'
            2. Run 'nv set system message post-login'
            3. Run 'nv set system message post-logout'
            4. Run 'nv show system message' and verify system messages are set
            5. Unset system message
            6. Run 'nv show system message' and verify systems messages are set to defaults
    """
    new_pre_login_msg = "Testing PRE LOGIN MESSAGE"
    new_post_login_msg = "Testing POST LOGIN MESSAGE"
    new_post_logout_msg = "Testing POST LOGOUT MESSAGE"
    system = System()

    try:
        with allure.step('Run set system message pre-login command and apply config'):
            system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value=f'"{new_pre_login_msg}"',
                               apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Run set system message post-login command and apply config'):
            system.message.set(op_param_name=SystemConsts.POST_LOGIN_MESSAGE, op_param_value=f'"{new_post_login_msg}"',
                               apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Run set system message post-logout command and apply config'):
            system.message.set(op_param_name=SystemConsts.POST_LOGOUT_MESSAGE, op_param_value=f'"{new_post_logout_msg}"',
                               apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify system messages are changed to new messages in show system'):
            verify_system_messages_with_retry(system, new_pre_login_msg, new_post_login_msg, new_post_logout_msg)

        with allure.step('Unset system message and apply config'):
            system.message.unset(apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify system messages are changed to default in show system'):
            TestToolkit.tested_api = ApiType.NVUE
            verify_system_messages_with_retry(system, devices.dut.pre_login_message,
                                              devices.dut.post_login_message,
                                              SystemConsts.POST_LOGOUT_MESSAGE_DEFAULT_VALUE)
            TestToolkit.tested_api = random_api

    finally:
        clear_system_messages(system, engines)


@pytest.mark.banner
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
def test_system_reload_for_system_message(engines, devices, random_api):
    """
    Run reload system  command and verify the system messages are changed to default
        Test flow:
            1. Run 'nv set system message pre-login'
            2. Run 'nv set system message post-login'
            3. Run 'nv set system message post-logout'
            4. Run 'nv show system message' and verify system messages are set
            5. Run system reload
            6. Run 'nv show system message' and verify systems messages are set to defaults
    """
    new_pre_login_msg = "Testing PRE LOGIN MESSAGE"
    new_post_login_msg = "Testing POST LOGIN MESSAGE"
    new_post_logout_msg = "Testing POST LOGOUT MESSAGE"
    system = System()

    try:
        with allure.step('Run set system message pre-login command and apply config'):
            system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value=f'"{new_pre_login_msg}"',
                               apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Run set system message post-login command and apply config'):
            system.message.set(op_param_name=SystemConsts.POST_LOGIN_MESSAGE, op_param_value=f'"{new_post_login_msg}"',
                               apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Run set system message post-logout command and apply config'):
            system.message.set(op_param_name=SystemConsts.POST_LOGOUT_MESSAGE, op_param_value=f'"{new_post_logout_msg}"',
                               apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify system messages are changed to new messages in show system'):
            verify_system_messages_with_retry(system, new_pre_login_msg, new_post_login_msg, new_post_logout_msg)

        with allure.step('Run system reload command and apply config'):
            reload_cmd_set = "nv action reboot system"
            # Reload system and wait until the system is ready
            DutUtilsTool.reload(engine=engines.dut, device=devices.dut, command=reload_cmd_set, confirm=True,
                                reboot_params=RebootParams(should_wait_till_system_ready=True)
                                ).verify_result()
            # Reconnect
            ssh_connection = ConnectionTool.create_ssh_conn(engines.dut.ip, engines.dut.username, engines.dut.password).get_returned_value()

        with allure.step('Verify system messages are changed to default in show system'):
            TestToolkit.tested_api = ApiType.NVUE
            verify_system_messages_with_retry(system, devices.dut.pre_login_message,
                                              devices.dut.post_login_message,
                                              SystemConsts.POST_LOGOUT_MESSAGE_DEFAULT_VALUE)
            TestToolkit.tested_api = random_api

    finally:
        clear_system_messages(system, engines)


@pytest.mark.banner
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus_only
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_post_logout_message_multiple_users(engines, devices, test_api, system_message_cleanup):
    """
    Description:
    Verify switching between different users should not affect logout msg.

    Steps:
    1. Configure user user1
    2. Configure post logout message
    3. Verify after logout custom message is as expected
    4. Login as cumulus user and verify the custom message
    5. Login as user 1 and after logout verify custom message
    """
    TestToolkit.tested_api = test_api
    new_post_logout_msg = "Testing POST LOGOUT MESSAGE"
    system = system_message_cleanup

    with allure.step('Create a new user '):
        user1_plain_password = generate_strong_password()
        salt = crypt.mksalt(crypt.METHOD_SHA512)
        user_local_hashpw = f"'{crypt.crypt(user1_plain_password, salt)}'"
        system.aaa.user.set_new_user(username='user1', role='nvue-admin', hashed_password=user_local_hashpw, apply=True)

    with allure.step('Run set system message post-logout command and apply config'):
        system.message.set(op_param_name=SystemConsts.POST_LOGOUT_MESSAGE, op_param_value=f'"{new_post_logout_msg}"',
                           apply=True, dut_engine=engines.dut).verify_result()

    with allure.step('Verify post logout messages in show system'):
        message_output = OutputParsingTool.parse_json_str_to_dictionary(system.message.show()).get_returned_value()

        ValidationTool.verify_field_value_in_output(message_output, SystemConsts.POST_LOGOUT_MESSAGE,
                                                    new_post_logout_msg).verify_result()

    with allure.step('Switch to cumulus user and verify post logout messages in show system'):

        _, _, cumulus_login_message = SshAuthenticator(engines.dut.username, engines.dut.password, engines.dut.ip).attempt_login_success(return_output=True)

        message_output = OutputParsingTool.parse_json_str_to_dictionary(system.message.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(message_output, 'post-logout',
                                                    new_post_logout_msg).verify_result()

    with allure.step('Switch back to user1 and verify post logout messages in show system'):
        _, _, user_login_message = SshAuthenticator('user1', user1_plain_password, engines.dut.ip).attempt_login_success(return_output=True)
        logger.info(user_login_message)

        user_output = OutputParsingTool.parse_json_str_to_dictionary(system.message.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(user_output, 'post-logout',
                                                    new_post_logout_msg).verify_result()


@pytest.mark.banner
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus_only
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_post_logout_message_after_nvued_restart(engines, devices, test_api, topology_obj, system_message_cleanup):
    """
    Description:
    Verify that after nvued service restart the post-logout message still works.

    Steps:
    1. Configure post-log out message
    2. Verify the output display configured message
    3. Do nvued service restart
    4. Log out from the command prompt by running "logout"
    5. Verify that the post-logout message is displayed after the logout command
    """
    # Set post-logout message
    TestToolkit.tested_api = test_api
    new_post_logout_msg = "Testing POST LOGOUT MESSAGE"
    system = system_message_cleanup

    with allure.step('Run set system message post-logout command and apply config'):
        system.message.set(op_param_name=SystemConsts.POST_LOGOUT_MESSAGE, op_param_value=f'"{new_post_logout_msg}"',
                           apply=True, dut_engine=engines.dut).verify_result()

    # Restart nvued service
    with allure.step('Restart nvued services'):
        engines.dut.run_cmd('sudo systemctl restart nvued')

    # Poll until nvued service is active
    with allure.step('Wait for nvued service to become active'):
        retry_call(
            lambda: 'active' in engines.dut.run_cmd('systemctl is-active nvued'),
            tries=12,
            delay=2,
            logger=logger
        )

    with allure.step('Verify system message persists after service restart'):
        message_output = OutputParsingTool.parse_json_str_to_dictionary(system.message.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(message_output, SystemConsts.POST_LOGOUT_MESSAGE,
                                                    new_post_logout_msg).verify_result()

    with allure.step('Logout from device and check for post-logout message'):
        # Use pexpect to capture logout message
        logout_output = capture_logout_message_via_ssh_with_pexpect(engines)
        logger.info(f"Logout output: {logout_output}")

        # Check for post-logout message in output
        assert new_post_logout_msg in logout_output, f"Expected Post-logout message: '{new_post_logout_msg}' not found in output"
        logger.info(f"Post-logout message found: {new_post_logout_msg}")


def capture_logout_message_via_ssh_with_pexpect(engines):
    """
    Helper function to capture logout message using pexpect with SSH
    """
    import pexpect

    ssh_cmd = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {engines.dut.username}@{engines.dut.ip}"

    try:
        child = pexpect.spawn(ssh_cmd, timeout=60)

        # Login
        child.expect("password:")
        child.sendline(engines.dut.password)
        child.expect(['$', '#'])

        # Send logout and capture everything before connection closes
        child.sendline('logout')
        child.expect([pexpect.EOF, 'closed', 'Connection.*closed'], timeout=30)

        output = child.before.decode('utf-8', errors='ignore')
        return output

    except Exception as e:
        logger.error(f"Failed to capture logout message: {e}")
        return ""
