import logging
from ngts.tools.test_utils import allure_utils as allure
import pytest
import random
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.system.User import User
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli


cumulus_owner = "yleibovichwe"


logger = logging.getLogger()


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
@pytest.mark.nvos_chipsim_ci
@pytest.mark.nvos_security_build
@pytest.mark.air
def test_show_user(engines, devices):
    """
    Run show system message command and verify the required message
        Test flow:
            1. run nv show system aaa user
            2. validate all fields have values
            3. run nv show system aaa user admin
            4. validate all fields have values
            5. run nv show system aaa user monitor
            6. validate all fields have values
    """
    system = System()
    users_output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.user.show()).get_returned_value()
    verify_users_default_values(users_output, devices.dut.user_fields)

    if SystemConsts.DEFAULT_USER_ADMIN in users_output.keys():
        admin_output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.user.user_id[SystemConsts.DEFAULT_USER_ADMIN].show()).get_returned_value()
        monitor_output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.user.user_id[SystemConsts.DEFAULT_USER_MONITOR].show()).get_returned_value()

        fields = [SystemConsts.USER_FULL_NAME, SystemConsts.USER_ROLE, SystemConsts.USER_STATE, SystemConsts.USER_HASHED_PASSWORD, SystemConsts.USER_PASSWORD]

        admin_values = [SystemConsts.USER_ADMIN_DEFAULT_FULL_NAME, SystemConsts.ROLE_CONFIGURATOR, SystemConsts.USER_STATE_ENABLED, SystemConsts.USER_PASSWORDS_DEFAULT_VALUE, SystemConsts.USER_PASSWORDS_DEFAULT_VALUE]

        monitor_values = [SystemConsts.USER_MONITOR_DEFAULT_FULL_NAME, SystemConsts.ROLE_VIEWER, SystemConsts.USER_STATE_ENABLED, SystemConsts.USER_PASSWORDS_DEFAULT_VALUE, SystemConsts.USER_PASSWORDS_DEFAULT_VALUE]

        verify_fields_values(SystemConsts.DEFAULT_USER_ADMIN, admin_output, fields, admin_values)

        verify_fields_values(SystemConsts.DEFAULT_USER_MONITOR, monitor_output, fields, monitor_values)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.air
@pytest.mark.nvos_chipsim_ci
def test_show_role(engines):
    """
    Run show system message command and verify the required message
        Test flow:
            1. run nv show system aaa role
            2. validate all fields have values
            3. run nv show system aaa role configurator
            4. validate all fields have values
            5. run nv show system aaa role viewer
            6. validate all fields have values
    """
    system = System(None)
    roles_output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.role.show()).get_returned_value()
    verify_roles_default_values(roles_output)

    configurator_output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.role.show(SystemConsts.ROLE_CONFIGURATOR)).get_returned_value()
    viewer_output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.role.show(SystemConsts.ROLE_VIEWER)).get_returned_value()

    labels = [SystemConsts.ROLE_GROUPS]
    configurator_values = [SystemConsts.ROLE_CONFIGURATOR_DEFAULT_GROUPS]
    viewer_values = [SystemConsts.ROLE_VIEWER_DEFAULT_GROUPS]

    verify_fields_values(SystemConsts.ROLE_CONFIGURATOR, configurator_output, labels, configurator_values)
    verify_fields_values(SystemConsts.ROLE_VIEWER, viewer_output, labels, viewer_values)


@pytest.mark.system
@pytest.mark.simx
def test_invalid_username(engines):
    """
    Run show system message command and verify the required message
        Test flow:
            1. generate invalid username save as <invalid_username>
            2. nv set system aaa user <invalid_username>
            3. run nv config diff
            4. verify it's empty
            5. generate valid username with length > max save as <invalid_length_username>
            6. nv set system aaa user <invalid_length_username>
            7. run nv config diff
            8. verify it's empty
    """
    system = System(None)
    with allure.step('verify we can not user invalid user name - user name is up to 32 letters, digits and start with letter or _ '):
        invalid_username = User.generate_username(is_valid=False)
        info = system.aaa.user.set(invalid_username, '').verify_result(False)
        assert "'{invalid_username}' is not a 'user-name'".format(invalid_username=invalid_username) in info, \
            'succeeded to set invalid username - not as expected'

    with allure.step('verify we can not user invalid user name - user name is up to 32 letters, digits and start with letter or _ '):
        random_length = random.randint(SystemConsts.USERNAME_MAX_LEN + 1, 100)
        invalid_username = User.generate_username(random_length=False, length=random_length)
        info = system.aaa.user.set(invalid_username, '').verify_result(False)
        assert "'{invalid_username}' is not a 'user-name'".format(invalid_username=invalid_username) in info, \
            'succeeded to set invalid username - not as expected'


@pytest.mark.system
@pytest.mark.simx
def test_set_state_default_user(engines):
    """
    Run show system message command and verify the required message
        Test flow:
            1. generate invalid username save as <invalid_username>
            2. nv set system aaa user <invalid_username>
            3. run nv config diff
            4. verify it's empty
    """
    output = System().aaa.user.user_id[SystemConsts.DEFAULT_USER_ADMIN].set(SystemConsts.USER_STATE, SystemConsts.USER_STATE_DISABLED, apply=True).verify_result(False)
    assert 'default administrator should not be disabled' in output, 'succeeded to set default user state - not as expected'


@pytest.mark.system
@pytest.mark.simx
def test_set_capability_default_user(engines):
    """
    Run show system message command and verify the required message
        Test flow:
            1. generate invalid username save as <invalid_username>
            2. nv set system aaa user <invalid_username>
            3. run nv config diff
            4. verify it's empty
    """
    output = System().aaa.user.user_id[SystemConsts.DEFAULT_USER_ADMIN].set(SystemConsts.ROLE_LABEL, SystemConsts.ROLE_VIEWER, apply=True).verify_result(False)
    assert 'role configuration change of default user is not allowed' in output, 'succeeded to set default user state - not as expected'


def verify_roles_default_values(roles_output):
    with allure.step('Check that default roles are exist configurator and viewer'):
        logging.info('Check that default roles are exist configurator and viewer')
        field_to_check = [SystemConsts.ROLE_CONFIGURATOR, SystemConsts.ROLE_VIEWER]
        ValidationTool.verify_field_exist_in_json_output(roles_output, field_to_check).verify_result()

        labels = [SystemConsts.ROLE_GROUPS]
        configurator_values = [SystemConsts.ROLE_CONFIGURATOR_DEFAULT_GROUPS]
        viewer_values = [SystemConsts.ROLE_VIEWER_DEFAULT_GROUPS]

        verify_fields_values(SystemConsts.ROLE_CONFIGURATOR, roles_output[SystemConsts.ROLE_CONFIGURATOR], labels, configurator_values)
        verify_fields_values(SystemConsts.ROLE_VIEWER, roles_output[SystemConsts.ROLE_VIEWER], labels, viewer_values)


def verify_users_default_values(users_output, field_to_check):
    with allure.step('Check that default users are exist admin and monitor'):
        logging.info('Check that default users are exist admin and monitor')
        ValidationTool.verify_field_exist_in_json_output(users_output, field_to_check).verify_result()

        if SystemConsts.DEFAULT_USER_ADMIN in users_output.keys():
            labels = [SystemConsts.USER_FULL_NAME, SystemConsts.USER_ROLE, SystemConsts.USER_STATE]
            admin_values = [SystemConsts.USER_ADMIN_DEFAULT_FULL_NAME, SystemConsts.ROLE_CONFIGURATOR, SystemConsts.USER_STATE_ENABLED]
            monitor_values = [SystemConsts.USER_MONITOR_DEFAULT_FULL_NAME, SystemConsts.ROLE_VIEWER, SystemConsts.USER_STATE_ENABLED]
            verify_fields_values(SystemConsts.DEFAULT_USER_ADMIN, users_output[SystemConsts.DEFAULT_USER_ADMIN], labels, admin_values)
            verify_fields_values(SystemConsts.DEFAULT_USER_MONITOR, users_output[SystemConsts.DEFAULT_USER_MONITOR], labels, monitor_values)


def verify_fields_values(user, user_output, fields, values):
    with allure.step('Check that default values for {user} are as expected'.format(user=user)):
        logging.info('Check that default values for {user} are as expected'.format(user=user))

        for (expected_label, expected_value) in zip(fields, values):
            logging.info('Check that default values for {user} are as expected'.format(user=user))
            ValidationTool.verify_field_value_in_output(user_output, expected_label, expected_value).verify_result()
