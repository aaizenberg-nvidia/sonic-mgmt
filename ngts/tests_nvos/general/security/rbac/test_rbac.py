import logging
import random
import pytest

from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.tests_nvos.general.security.rbac.helpers import change_class_permissions, verify_user_permissions_on_interface, \
    verify_user_permissions_on_system, verify_user_permissions_on_denied_interface, run_commands_on_system, run_commands_on_interface, \
    create_user_connection, verify_groups, verify_rbac_classes_in_role
from ngts.tests_nvos.system.aaa.helpers import create_new_user

from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import ApiType, SystemConsts, RbacConsts, UfmMadConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.mark.system
@pytest.mark.rbac
@pytest.mark.security_ci
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_rbac_interface_single_allow(engines, test_api):
    """
    Verify RBAC for a single interface command path
    Test flow:
        1. Configure user access permissions for a single interface
        2. Test different permission levels (ro, rw, act, all)
        3. Verify set/unset/show operations for permitted configs
        4. Verify user can only access permissible command paths
    """
    TestToolkit.tested_api = test_api
    system = System(None)
    test_class_name = "TestInterfaceClass"
    test_role_name = "TestRole"
    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
    TestToolkit.update_tested_ports([selected_port])
    interface_path = f"/interface/{selected_port.name}/"

    system.aaa.class_rbac.set_new_class(test_class_name, RbacConsts.ALLOW, interface_path)
    system.aaa.role.set_new_role(test_role_name, test_class_name, apply=True)
    test_user, test_password = create_new_user(test_role_name, apply=True)

    permission = random.choice(RbacConsts.PERMISSION_LEVELS)
    with allure.step(f"Testing permission level: {permission}"):
        change_class_permissions(system, test_class_name, interface_path, permission)
        verify_user_permissions_on_interface(engines, test_user, test_password, selected_port, permission, fail_on_eth0=True)


@pytest.mark.system
@pytest.mark.rbac
@pytest.mark.security_ci
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_rbac_multiple_users(engines, devices, test_api):
    """
    Verify RBAC for a single interface command path
    Test flow:
        1. Configure user access permissions for a single interface
        2. Test different permission levels (ro, rw, act, all)
        3. Verify set/unset/show operations for permitted configs
        4. Verify user can only access permissible command paths
    """
    TestToolkit.tested_api = test_api
    system = System(None)
    test_class_name = "TestImageClass"
    test_role_sys_mgr = "SystemMgr"
    test_role_sys_mgr_1 = "SystemMgr1"
    system_path = "/system/"

    system.aaa.class_rbac.set_new_class(test_class_name, RbacConsts.ALLOW, system_path)
    system.aaa.role.set_new_role(test_role_sys_mgr, test_class_name)
    system.aaa.role.set_new_role(test_role_sys_mgr_1, test_class_name, apply=True)
    test_user1, test_password1 = create_new_user(test_role_sys_mgr)
    test_user2, test_password2 = create_new_user(test_role_sys_mgr)
    test_user3, test_password3 = create_new_user(test_role_sys_mgr_1)
    test_user4, test_password4 = create_new_user(test_role_sys_mgr_1, apply=True)
    users_data = [
        (test_user1, test_password1),
        (test_user2, test_password2),
        (test_user3, test_password3),
        (test_user4, test_password4)
    ]
    permission = random.choice(RbacConsts.PERMISSION_LEVELS)
    with allure.step(f"Testing user permission: {permission}"):
        for tested_user, tested_password in users_data:
            with allure.step(f"Testing user: {tested_user}"):
                change_class_permissions(system, test_class_name, system_path, permission)
                verify_user_permissions_on_system(engines, devices, tested_user, tested_password, permission)


@pytest.mark.system
@pytest.mark.rbac
@pytest.mark.security_ci
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_rbac_interface_single_deny(engines, test_api):
    """
    Test RBAC (Role-Based Access Control) for a single interface with deny permission.

    This test verifies that:
    1. A user can be denied access to a specific interface (eth0) while having access to other interfaces.
    2. The deny permission takes precedence over allow permission when both are applied.

    Test flow:
    1. Set up RBAC classes:
       - An allow class for all interfaces
       - A deny class specifically for the eth0 interface
    2. Create a role with both allow and deny classes
    3. Create a test user with the new role
    4. Verify that the user cannot access the denied interface (eth0)
    5. Verify that the user can access other interfaces
    """
    TestToolkit.tested_api = test_api
    system = System(None)
    test_class_name = "TestInterfaceClass"
    deny_eth0_class_name = "TestDenyEth0Class"
    test_role_name = "TestRole"
    selected_port_deny = Port(UfmMadConsts.MGMT_PORT0)
    TestToolkit.update_tested_ports([selected_port_deny])
    interface_path = "/interface"
    interface_path_deny = f"/interface/{selected_port_deny.name}/"

    system.aaa.class_rbac.set_new_class(test_class_name, RbacConsts.ALLOW, interface_path, permission=RbacConsts.ALL)
    system.aaa.class_rbac.set_new_class(deny_eth0_class_name, RbacConsts.DENY, interface_path_deny)
    system.aaa.role.set_new_role(test_role_name, test_class_name)
    system.aaa.role.set_new_role(test_role_name, deny_eth0_class_name, apply=True)
    test_user, test_password = create_new_user(test_role_name, apply=True)

    with allure.step(f"Testing {deny_eth0_class_name} class with permission level: {RbacConsts.ALL}"):
        change_class_permissions(system, deny_eth0_class_name, interface_path_deny, RbacConsts.ALL)
        verify_user_permissions_on_denied_interface(engines, test_user, test_password, selected_port_deny, RbacConsts.ALL)

    # testing allowed class for random interface
    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
    TestToolkit.update_tested_ports([selected_port])
    verify_user_permissions_on_interface(engines, test_user, test_password, selected_port, RbacConsts.ALL)


@pytest.mark.system
@pytest.mark.rbac
@pytest.mark.security_ci
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_rbac_role_update(engines, devices, test_api):
    """
    Test RBAC role updates.

    This test verifies that when classes within a role get updated for a user,
    the user's permissions change accordingly.

    Test flow:
    1. Set up initial configuration with a system class and an interface class.
    2. Create a role with only the system class and assign it to a test user.
    3. Verify initial permissions (full access to system, no access to interface).
    4. Update the role by adding the interface class.
    5. Verify updated permissions (full access to both system and interface).
    6. Update the role by removing the system class.
    7. Verify final permissions (full access to interface, no access to system).

    Args:
        engines: The test engines.
        devices: The devices under test.
        test_api (ApiType): The API type to be tested.
    """
    TestToolkit.tested_api = test_api
    system = System()
    test_system_class = "TestSystemClass"
    test_interface_class = "TestInterfaceClass"
    test_role_name = "TestRole"
    system_path = "/system/"
    interface_path = "/interface"

    system.aaa.class_rbac.set_new_class(test_system_class, RbacConsts.ALLOW, system_path, permission=RbacConsts.ALL)
    system.aaa.class_rbac.set_new_class(test_interface_class, RbacConsts.ALLOW, interface_path, permission=RbacConsts.ALL)
    system.aaa.role.set_new_role(test_role_name, test_system_class, apply=True)
    test_user, test_password = create_new_user(test_role_name, apply=True)

    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
    TestToolkit.update_tested_ports([selected_port])
    verify_user_permissions_on_system(engines, devices, test_user, test_password, RbacConsts.ALL)
    interface_results = run_commands_on_interface(selected_port, create_user_connection(engines, test_user, test_password))
    assert not any(interface_results.values()), "User should not be able to perform any actions on interface"

    with allure.step(f"Updating role by adding a new class {test_interface_class}"):
        system.aaa.role.role_id[test_role_name].class_rbac.set(test_interface_class, apply=True, ask_for_confirmation=True)
        verify_user_permissions_on_interface(engines, test_user, test_password, selected_port, RbacConsts.ALL)
        verify_user_permissions_on_system(engines, devices, test_user, test_password, RbacConsts.ALL)

    with allure.step(f"Updating role by removing a class {test_system_class}"):
        system.aaa.role.role_id[test_role_name].class_rbac.unset(test_system_class, apply=True, ask_for_confirmation=True)
        verify_user_permissions_on_interface(engines, test_user, test_password, selected_port, RbacConsts.ALL)
        system_results = run_commands_on_system(create_user_connection(engines, test_user, test_password))
        assert not any(system_results.values()), "User should not be able to perform any actions on system path"


@pytest.mark.system
@pytest.mark.rbac
@pytest.mark.security_ci
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_rbac_unset_used_class(engines, test_api):
    """
    Test RBAC unset commands for used classes and roles.

    This test verifies that:
    1. A class that is being used by a role cannot be unset.
    2. A role that is being used by a user cannot be unset.
    3. When a user's role is unset, the user defaults to the monitor role.

    Args:
        engines: The test engines.
        test_api (ApiType): The API type to be tested.

    Test flow:
    1. Set up a test class, role, and user.
    2. Attempt to unset the class and role while in use (should fail).
    3. Unset the user's role and verify it defaults to monitor.
    4. Clean up all created resources.
    """
    TestToolkit.tested_api = test_api
    system = System(None)
    test_class_name = "TestClass"
    test_role_name = "TestRole"
    system_path = "/system"

    system.aaa.class_rbac.set_new_class(test_class_name, RbacConsts.ALLOW, system_path, RbacConsts.READ_WRITE)
    system.aaa.role.set_new_role(test_role_name, test_class_name, apply=True)
    test_user, test_password = create_new_user(test_role_name, apply=True)

    with allure.step("Testing unset used class in role"):
        system.aaa.class_rbac.class_id[test_class_name].unset(apply=True).verify_result(False)

    with allure.step("Testing unset used class in role"):
        system.aaa.role.role_id[test_role_name].unset(apply=True).verify_result(False)

    with allure.step("Testing unset single role on user - default role should be monitor"):
        system.aaa.user.user_id[test_user].unset(op_param=RbacConsts.ROLE, apply=True).verify_result()
        test_user_output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.user.user_id[test_user].show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(test_user_output, SystemConsts.USER_ROLE, SystemConsts.DEFAULT_USER_MONITOR)


@pytest.mark.system
@pytest.mark.rbac
@pytest.mark.security_ci
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_rbac_class_config_conflict(engines, test_api):
    """
    Verify that conflicting RBAC class configurations are handled correctly.

    This test checks the following scenarios:
    1. Attempting to apply conflicting classes to a role simultaneously fails.
    2. The role is not created when conflicting classes are applied together.
    3. When applying conflicting classes one by one, the first class is kept and the second is rejected.

    Test steps:
    1. Configure two conflicting RBAC classes (one denying all interface access, one allowing access to eth0).
    2. Attempt to apply both classes to a role simultaneously and verify it fails.
    3. Verify the role was not created after the failed attempt.
    4. Apply the denying class to the role, then attempt to add the conflicting allowing class.
    5. Verify that only the first (denying) class remains in the role configuration.
    """
    TestToolkit.tested_api = test_api
    system = System(None)
    test_role_name = "TestConflictRole"
    deny_class_name = "DenyInterfaceClass"
    allow_class_name = "AllowInterfaceClass"
    more_than_parent_class_name = "MoreThanParentClass"
    interface_path = "/interface"
    system_path = "/system"
    aaa_path = "/system/aaa"

    with allure.step("Configure conflicting permissions (child has more permissions than parent) in the same class - should fail"):
        system.aaa.class_rbac.set_new_class(more_than_parent_class_name, RbacConsts.ALLOW, system_path, permission=RbacConsts.READ_ONLY)
        system.aaa.class_rbac.class_id[more_than_parent_class_name].command_path.command_path_id[aaa_path].set(op_param_name=RbacConsts.PERMISSION, op_param_value=RbacConsts.READ_WRITE, apply=True).verify_result(False)

    with allure.step("Configure conflicting classes and role"):
        system.aaa.class_rbac.set_new_class(deny_class_name, RbacConsts.DENY, interface_path, permission=RbacConsts.ALL)
        system.aaa.class_rbac.set_new_class(allow_class_name, RbacConsts.ALLOW, "/interface/eth0", permission=RbacConsts.ALL, apply=True)

    with allure.step("Attempt to apply conflicting configuration both together - should fail"):
        system.aaa.role.set_new_role(test_role_name, deny_class_name)
        res_obj = system.aaa.role.role_id[test_role_name].class_rbac.set(allow_class_name, apply=True).verify_result(False)
        assert RbacConsts.CONFLICT_ERR_MSG in res_obj, f"expected conflict msg got: {res_obj}"

    with allure.step("Verify role was not created"):
        system.aaa.role.role_id[test_role_name].show(should_succeed=False)

    with allure.step("Attempt to apply conflicting configuration one by one - should fail but keep the first"):
        NvueGeneralCli.detach_config(engines.dut)

        system.aaa.role.set_new_role(test_role_name, deny_class_name, apply=True)
        res_obj = system.aaa.role.role_id[test_role_name].class_rbac.set(allow_class_name, apply=True).verify_result(False)
        assert RbacConsts.CONFLICT_ERR_MSG in res_obj, f"expected conflict msg got: {res_obj}"

    with allure.step(f"Verify role has only the {deny_class_name} class"):
        verify_rbac_classes_in_role(system, test_role_name, [deny_class_name, allow_class_name], [True, False])


@pytest.mark.system
@pytest.mark.rbac
@pytest.mark.security_ci
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_rbac_user_add_remove_class_interaction(engines, test_api):
    """
    Test flow:
    1. As a root user, add a class to a role assigned to a logged-in regular user using NVUE.
    2. Check that the user is disconnected.
    3. Let the user log in again and verify the new permissions.
    4. Rerun the flow with removing a class
    """
    TestToolkit.tested_api = test_api
    system = System(None)
    test_system_class = "TestSystemClass"
    test_interface_class = "TestInterfaceClass"
    test_role_name = "TestRole"
    system_path = "/system/"
    interface_path = "/interface"

    system.aaa.class_rbac.set_new_class(test_system_class, RbacConsts.ALLOW, system_path, RbacConsts.READ_WRITE)
    system.aaa.class_rbac.set_new_class(test_interface_class, RbacConsts.ALLOW, interface_path, RbacConsts.ALL)
    system.aaa.role.set_new_role(test_role_name, test_system_class, apply=True)
    test_user, test_password = create_new_user(test_role_name, apply=True)

    with allure.step(f"verify new class {test_system_class} in role"):
        verify_rbac_classes_in_role(system, test_role_name, [test_system_class])

    user_engine = create_user_connection(engines, test_user, test_password)
    verify_groups(user_engine, test_user, [test_system_class])

    with allure.step(f"Updating role by adding a class {test_interface_class}"):
        system.aaa.role.role_id[test_role_name].class_rbac.set(test_interface_class, apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Verify the user is disconnected after permission change"):
        ConnectionTool.is_connected(engines.dut, test_user).verify_result(False)

    with allure.step("Allow the user to log in again and verify new permissions"):
        user_engine = create_user_connection(engines, test_user, test_password)
        verify_rbac_classes_in_role(system, test_role_name, [test_system_class, test_interface_class], user_engine=user_engine)
        verify_groups(user_engine, test_user, [test_system_class, test_interface_class])

    with allure.step(f"Updating role by removing a class {test_interface_class}"):
        system.aaa.role.role_id[test_role_name].class_rbac.unset(test_interface_class, apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Verify the user is disconnected after permission change"):
        assert ConnectionTool.is_connected(engines.dut, test_user).verify_result(False)

    with allure.step("Allow the user to log in again and verify new permissions"):
        user_engine = create_user_connection(engines, test_user, test_password)
        verify_rbac_classes_in_role(system, test_role_name, [test_system_class, test_interface_class], [True, False], user_engine=user_engine)
        verify_groups(user_engine, test_user, [test_system_class])


@pytest.mark.system
@pytest.mark.rbac
@pytest.mark.security_ci
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_rbac_limitations(engines, test_api):
    """
    Test Objective:
    This test case verifies that the scaled RBAC configuration with the maximum allowed classes per role
    and command paths per class can be applied in one go without timing out.

    Test Flow:
    1. Define 65 classes.
    2. Assign 64 classes to a new role using a loop. (RbacConsts.CLASS_LIMIT = 64)
    3. Attempt to assign one more class and verify that an error is returned.

    """
    TestToolkit.tested_api = test_api
    test_role_name = "TestRole"
    classes = [f"TestClass{i}" for i in range(RbacConsts.CLASS_LIMIT + 1)]  # Define 1 extra class
    system = System()
    system_path = "/system/"

    with allure.step("Defining 65 classes"):
        for class_name in classes:
            system.aaa.class_rbac.set_new_class(class_name, RbacConsts.ALLOW, system_path, RbacConsts.READ_WRITE)

    with allure.step("Assigning 64 classes to a new role using a loop"):
        system.aaa.role.set_new_role(test_role_name, classes[0])  # Create the role first
        for i, class_name in enumerate(classes[1:RbacConsts.CLASS_LIMIT]):
            system.aaa.role.role_id[test_role_name].class_rbac.set(class_name, apply=(i + 1 == RbacConsts.CLASS_LIMIT))

    with allure.step("Attempting to assign one more class and verifying error"):
        system.aaa.role.role_id[test_role_name].class_rbac.set(classes[RbacConsts.CLASS_LIMIT], apply=True).verify_result(False)
