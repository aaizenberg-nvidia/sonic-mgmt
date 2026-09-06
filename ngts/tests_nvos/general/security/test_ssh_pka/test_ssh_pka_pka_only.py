import logging
import pytest
import time

from ngts.tests_nvos.general.security.security_test_tools.tool_classes.AuthVerifier import PKAAuthVerifier, SshAuthVerifier
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.SecuritySshTool import SecuritySshTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.tests_nvos.general.security.security_test_tools.constants import AddressingType
from ngts.tests_nvos.general.security.ssh_hardening.constants import SshHardeningConsts
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE

from ngts.tests_nvos.general.security.test_ssh_pka.helpers import _generate_new_key, keys_path

logger = logging.getLogger(__name__)


@pytest.mark.security
@pytest.mark.parametrize('addressing_type', [AddressingType.IPV4, AddressingType.IPV6])
def test_ssh_pka_positive_flow(engines, addressing_type, generate_new_admin_keys, dut_ipv6_addr):
    """
    @summary: verify the positive flow of connection using pka keys for admin and new user with monitor abilities.
    in this case we also verify the two ways of adding new key.

    Test Flow:
        - Create new non-default“monitor” user..	Save as <new_user>
        - Generate key id	Save as <random_key_id>
        - Generate two new keys(first random, second rsa)	Save as: <key_id>, <key_type>, <private_key_path>, <key_id_2>, <key_type_2> = default, <private_key_path_2>
        - Run who save sessions count as <admin_sessions_before_testing>
        - Run who save sessions count as <new_user_sessions_before_testing>
        - Run nv show system aaa 	Verify ssh field is empty for all users
        - Run nv show system aaa user admin	Verify no ssh-keys
        - Run nv set system aaa user admin ssh authorized-key <random_key_id>
        - Run nv set system aaa user admin ssh authorized-key <random_key_id> key <key_id>
        - Run nv set system aaa user <user-id> ssh authorized-key admin type <key_type>
        - Run nv config apply	Verify result - Should succeed
        - Run nv set system aaa user <new_user> ssh authorized-key <random_key_id> key <key_id>  + apply - Verify result - Should succeed
        - Run nv show system aaa 	ssh keys for admin and new user
        - Run nv show system aaa user new_user - <key_id_2> <key_type_2>
        - Run nv show system aaa user admin ssh authorized-key <key_id> - <key_id> <key_type>
        - Run nv show system aaa user admin ssh authorized-key <random_key_id> - <key_id> <key_type>
        - Login using: ssh -i ~/.<private_key_path>  admin@hostname - Verify result - Should succeed
        - Login using: ssh -i ~/.<private_key_path_2>  <new_user>@hostname - Verify result - Should succeed
        - Login using: ssh -i ~/.<private_key_path_2>  admin@hostname - Verify result - Should fail
        - Run who admin	- verify that session - <admin_sessions_before_testing> = 2
        - Run who <new_user>	- verify that session - <new_user_sessions_before_testing> = 2 save connection as <new_user_session>
        - run user ability test using <new_user_session> .. we need to try set, actions and verify we can't do so, and we can run show commands
        - Run nv unset system aaa user admin ssh authorized-key <random_key_id>  + apply
        - Login using: ssh -i ~/.<private_key_path>  admin@hostname - Verify result - Should fail
        - Run nv unset system aaa user
        - Create same user again
        - Run nv show system aaaa user <new_user>	Keys_list = empty
        - Login using: ssh -i ~/.<private_key_path_2>  <new_user>@hostname  - Verify result - Should fail
    """
    try:
        with allure.step("create system"):
            system = System()

        with allure.step("create new user with monitor abilities"):
            monitor_user, monitor_password = system.aaa.user.set_new_user(role=SystemConsts.DEFAULT_USER_MONITOR,
                                                                          apply=True)

        with allure.step("generate valid key id"):
            random_key_id = system.aaa.user.generate_username()
            admin_key_obj = system.aaa.user.user_id['admin'].ssh.authorized_key.key_id[random_key_id]

        with allure.step(f"generate two new keys one for admin and one for {monitor_user}"):
            admin_key, admin_key_type, admin_private_key_path = generate_new_admin_keys
            monitor_key, monitor_key_type, monitor_private_key_path = _generate_new_key(engines.dut, monitor_user,
                                                                                        'ssh-rsa')

        with allure.step(f"get open sessions count for both admin and {monitor_user}"):
            admin_sessions_before_testing = system.aaa.user.get_ssh_session_count(engine=engines.dut, username='admin')
            monitor_sessions_before_testing = system.aaa.user.get_ssh_session_count(engine=engines.dut, username=monitor_user)

        with allure.step("test PKA functionality"):
            with allure.independent_step("verify the default output of the show commands"):
                admin_ssh_output = OutputParsingTool.parse_json_str_to_dictionary(
                    system.aaa.user.user_id['admin'].ssh.show()).get_returned_value()
                monitor_authorized_key_output = system.aaa.user.user_id[monitor_user].ssh.authorized_key.show()
                ValidationTool.verify_field_value_in_output(output_dictionary=admin_ssh_output,
                                                            field_name='authorized-key',
                                                            expected_value='{}').verify_result()
                assert monitor_authorized_key_output == '{}', "the authorized key field should be empty"

            with allure.independent_step("add new public key using three set commands to admin user"):
                admin_key_obj.set()
                admin_key_obj.set(op_param_name='key', op_param_value=admin_key)
                admin_key_obj.set(op_param_name='type', op_param_value=admin_key_type, apply=True).verify_result()

            with allure.independent_step(f"add new public key using one command for {monitor_user}"):
                system.aaa.user.user_id[monitor_user].ssh.authorized_key.key_id[random_key_id].set(op_param_name='key',
                                                                                                   op_param_value=monitor_key,
                                                                                                   apply=True).verify_result()

            with allure.independent_step("verify the show commands output after adding new keys"):
                expected_authorized_key_dict = {"key": '*', "type": 'ssh-rsa'}

                with allure.independent_step(f"verify nv show system aaa user {monitor_user} command"):
                    user_output = OutputParsingTool.parse_json_str_to_dictionary(
                        system.aaa.user.user_id[monitor_user].show()).get_returned_value()
                    ValidationTool.verify_field_value_in_output(output_dictionary=user_output, field_name='ssh',
                                                                expected_value={'authorized-key': {
                                                                    random_key_id: expected_authorized_key_dict}}).verify_result()

                with allure.independent_step(
                        f"verify nv show system aaa user {monitor_user} ssh authorized key command"):
                    authorized_key_output = OutputParsingTool.parse_json_str_to_dictionary(
                        system.aaa.user.user_id[monitor_user].ssh.authorized_key.show()).get_returned_value()
                    ValidationTool.verify_field_value_in_output(output_dictionary=authorized_key_output,
                                                                field_name=random_key_id,
                                                                expected_value=expected_authorized_key_dict).verify_result()

                with allure.independent_step(
                        f"verify nv show system aaa user {monitor_user} ssh authorized key {random_key_id} command"):
                    random_key_id_output = OutputParsingTool.parse_json_str_to_dictionary(
                        system.aaa.user.user_id[monitor_user].ssh.authorized_key.show(
                            op_param=random_key_id)).get_returned_value()
                    ValidationTool.validate_fields_values_in_output(output_dict=random_key_id_output,
                                                                    expected_fields=list(
                                                                        expected_authorized_key_dict.keys()),
                                                                    expected_values=list(
                                                                        expected_authorized_key_dict.values())).verify_result()

            hostname = engines.dut.ip if addressing_type == AddressingType.IPV4 else dut_ipv6_addr
            with PKAAuthVerifier(username='admin', private_key_path=admin_private_key_path,
                                 hostname=hostname, engines=engines) as admin_session_obj, \
                    PKAAuthVerifier(username=monitor_user, private_key_path=monitor_private_key_path,
                                    hostname=hostname, engines=engines) as monitor_session_obj:
                with allure.independent_step("try to connect using the keys"):
                    admin_session_obj.verify_authentication(True)
                    monitor_session_obj.verify_authentication(True)

                with allure.independent_step(f"verify sessions count for both admin and {monitor_user}"):
                    admin_sessions_after_testing = system.aaa.user.get_ssh_session_count(engine=engines.dut, username='admin')
                    monitor_sessions_after_testing = system.aaa.user.get_ssh_session_count(engine=engines.dut, username=monitor_user)

                    with allure.independent_step("verify sessions count for admin"):
                        assert admin_sessions_after_testing - admin_sessions_before_testing == 1, (
                            f"after connection using key we expect more sessions for admin, the sessions count "
                            f"before testing was {admin_sessions_before_testing} and after connecting with the "
                            f"key it's {admin_sessions_after_testing}"
                        )

                    with allure.independent_step(f"verify sessions count for {monitor_user}"):
                        assert monitor_sessions_after_testing - monitor_sessions_before_testing == 1, (
                            f"after connection using key we expect more sessions for {monitor_user}, the "
                            f"sessions count before testing was {monitor_sessions_before_testing} and after "
                            f"connecting with the key it's {monitor_sessions_after_testing}"
                        )

                with allure.independent_step("verify users ability using the new connection session"):
                    with allure.independent_step("verify admin ability"):
                        admin_session_obj.verify_authorization(user_is_admin=True)
                    with allure.independent_step("verify monitor ability"):
                        monitor_session_obj.verify_authorization(user_is_admin=False)

            with allure.independent_step("verify functionality  after unset admin key"):
                with allure.step(f"unset {random_key_id} for admin"):
                    admin_key_obj.unset(apply=True).verify_result()

                with allure.step("verify we can not connect using key"):
                    with PKAAuthVerifier(username='admin', private_key_path=admin_private_key_path, hostname=hostname, engines=engines) as pka_connection:
                        pka_connection.verify_authentication(False)

                with allure.step("delete user"):
                    system.aaa.user.user_id[monitor_user].unset(apply=True).verify_result()

                with allure.step("create same user again"):
                    monitor_user, monitor_password = system.aaa.user.set_new_user(username=monitor_user,
                                                                                  role=SystemConsts.DEFAULT_USER_MONITOR,
                                                                                  apply=True)

                with allure.step("verify we can not connect using key and keys output is empty"):
                    with PKAAuthVerifier(username=monitor_user, private_key_path=monitor_private_key_path,
                                         hostname=hostname, engines=engines) as pka_connection:
                        pka_connection.verify_authentication(False)
                    authorized_key_output = system.aaa.user.user_id[monitor_user].ssh.authorized_key.show()
                    assert authorized_key_output == '{}', "the authorized key field should be empty"
    finally:
        with allure.step(f"delete keys for {monitor_user}"):
            SecuritySshTool.rm_auth_keypair(f"{keys_path}/{monitor_user}")


@pytest.mark.security
def test_ssh_pka_invalid_values(engines, generate_new_admin_keys):
    """
    @summary:

    Test Flow:
        - Generate new key	Save as: <key_string>, <key_type>, <private_key_path>
        - Run nv set system aaa user <user-id> ssh authorized-key <random_key_id>  + apply	Verify result - Should fail, err_msg = key string is missing
        - Run nv set system aaa user admin ssh authorized-key <random_key_id> key <key_type>  + apply	Verify result - Should fail, err_msg = key string is missing
        - generate new keys - now we have public key that should not work with the new private key
        - Login using: ssh -i ~/.<private_key_path>  admin@hostname - Verify result - Should fail
        - Run nv set system aaa user <user-id> ssh authorized-key admin type <dsa> 	Err msg: Invalid type – this should fail before applying the configuration
        - Run nv set system aaa user admin ssh authorized-key <invalid_key_id> key <new_key_string> + apply	Verify result - Should fail - err_msg = invalid key id=use generate invalid username method …
    """
    with allure.step("create system"):
        err_msg = "must contain type and key"
        system = System()
        key_id = 'new_key'
        admin_key_obj = system.aaa.user.user_id['admin'].ssh.authorized_key.key_id[key_id]

    with allure.step("generate new key for admin"):
        admin_key, admin_key_type, admin_private_key_path = generate_new_admin_keys

    try:
        with allure.step("testing bad flows"):
            with allure.independent_step("Bad Flow: try to set only key id and key type"):
                set_result_obj = admin_key_obj.set(apply=True).ignore_result()
                assert err_msg in set_result_obj.info, "test should fail because we can't configure new key with out public key"
                assert not set_result_obj.result, "result should be false"

                set_result_obj = admin_key_obj.set(op_param_name='type', op_param_value=admin_key_type, apply=True).ignore_result()
                assert err_msg in set_result_obj.info, "test should fail because we can't configure new key with out public key"
                assert not set_result_obj.result, "result should be false"

            with allure.independent_step("add the key for admin user"):
                admin_key_obj.set(op_param_name='key', op_param_value=admin_key, apply=True).verify_result()

            with allure.independent_step(
                    "generate new key for admin and verify we can not connect unless we change public key"):
                new_admin_key, new_admin_key_type, new_admin_private_key_path = _generate_new_key(engines.dut, 'admin')
                with PKAAuthVerifier(username='admin', private_key_path=new_admin_private_key_path, hostname=engines.dut.ip, engines=engines) as pka_connection:
                    pka_connection.verify_authentication(False)

            with allure.independent_step("Bad flow: try to set invalid key type"):
                admin_key_obj.set(op_param_name='type', op_param_value='dsa',
                                  expected_str="Error: 'dsa' is not one of [")

            with allure.independent_step("Bad flow: try to set invalid key id"):
                invalid_key = 'Invalid@'
                system.aaa.user.user_id['admin'].ssh.authorized_key.key_id[invalid_key].set(
                    expected_str="Error: 'Invalid@' is not a 'item-name'. Letters and digits, underscores and dashes are allowed, starting with a letter or digit.")
    finally:
        with allure.step("delete keys for admin"):
            admin_key_obj.unset(apply=True)


@pytest.mark.dut_reboot
@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.security
def test_ssh_pka_after_reboot_system(engines, generate_new_admin_keys):
    """
    Verify that we can connect using pka after reboot only after save

    1.	set new admin key
    2.	reboot
    3.  verify we can't connect and can't find key in show command
    5.  set new admin key (and save)
    6.  reboot
    7.  verify we can connect and key in show command
    """
    with allure.step("create system"):
        system = System()
        key_id = 'new_key'
        admin_key_obj = system.aaa.user.user_id['admin'].ssh.authorized_key.key_id[key_id]
        admin_key, admin_key_type, admin_private_key_path = generate_new_admin_keys

    with allure.step("test PKA functionality after reboot"):
        with allure.independent_step(
                "verify that we cannot connect using the key after a reboot if the configuration was not saved"):
            with allure.step("set public key"):
                admin_key_obj.set(op_param_name='key', op_param_value=admin_key, apply=True).verify_result()
                admin_key_obj.set(op_param_name='type', op_param_value=admin_key_type, apply=True).verify_result()

            with allure.step('reboot the system'):
                system.action_reboot('force').verify_result()

            with allure.independent_step("verify we can not connect using key"):
                with PKAAuthVerifier(username='admin', private_key_path=admin_private_key_path, hostname=engines.dut.ip, engines=engines) as pka_connection:
                    pka_connection.verify_authentication(False)

            with allure.independent_step("verify show command"):
                admin_key_obj.show(should_succeed=False)

        with allure.independent_step(
                "verify that we can connect using the key after a reboot if the configuration was saved"):
            with allure.step("set public key"):
                admin_key_obj.set(op_param_name='key', op_param_value=admin_key, apply=True).verify_result()
                admin_key_obj.set(op_param_name='type', op_param_value=admin_key_type, apply=True).verify_result()

            with allure.step('save config'):
                NvueGeneralCli.save_config(engines.dut)

            with allure.step('reboot the system'):
                system.action_reboot('force').verify_result()

            with allure.independent_step("verify we can connect using key"):
                with PKAAuthVerifier(username='admin', private_key_path=admin_private_key_path, hostname=engines.dut.ip, engines=engines) as admin_session_obj:
                    admin_session_obj.verify_authentication(True)

            with allure.independent_step("verify show command"):
                admin_key_obj.show(should_succeed=True)


@pytest.mark.security
def test_ssh_pka_expired_password(engines, generate_new_admin_keys):
    """
    Verify that we can connect using the key without asking for new password if the password expired

    1. create new user
    2. generate new keys
    3. add keys to the new user
    4. run sudo chage -d 0 <new_user> to expire the password
    5. verify we can connect using the key normally (not asking for new password)
    """
    with allure.step("create system"):
        system = System()

    with allure.step("create new user"):
        new_user, new_user_password = system.aaa.user.set_new_user(apply=True)

    with allure.step(f"generate new key for {new_user}"):
        new_user_key, new_user_key_type, new_user_private_key_path = _generate_new_key(engines.dut, new_user)

    with allure.step(f"add key to {new_user}"):
        system.aaa.user.user_id[new_user].ssh.authorized_key.key_id['expired_password'].set(op_param_name='key',
                                                                                            op_param_value=new_user_key,
                                                                                            apply=True).verify_result()
        system.aaa.user.user_id[new_user].ssh.authorized_key.key_id['expired_password'].set(op_param_name='type',
                                                                                            op_param_value=new_user_key_type,
                                                                                            apply=True).verify_result()

    with allure.step("change the password expire date"):
        engines.dut.run_cmd(f"sudo chage -d 0 {new_user}")

    with allure.step("verify we can connect normally and no new password asked for"):
        with PKAAuthVerifier(username=new_user, private_key_path=new_user_private_key_path, hostname=engines.dut.ip, engines=engines) as new_user_session_obj:
            new_user_session_obj.verify_authentication(True)

    with allure.step(f"delete keys for {new_user}"):
        SecuritySshTool.rm_auth_keypair(f"{keys_path}/{new_user}")


@pytest.mark.security
def test_ssh_pka_connections_stress(engines):
    """
    Verify the connection timeout after more than 20 connection with different pka types

    1.	generate 4 keys of each pka type (we have 5 different types)
    2.  connect 20 times and check the time it takes to login for each connection
    """
    with allure.step("create system"):
        system = System()
        threshold = 1
        bad_connection_timing = []

    try:
        with allure.step("generate 20 different keys for admin"):
            public_keys_list = []
            private_keys_paths_list = []
            keys_list = list(SshHardeningConsts.PUBLIC_KEY_LENGTH_DICT.keys())
            for i in range(4):
                for key in keys_list:
                    public_key, key_type, private_path = _generate_new_key(engine=engines.dut,
                                                                           user_name=f'admin_{i}_{key}', key_type=key)
                    private_keys_paths_list.append(private_path)
                    public_keys_list.append(public_key)

        with allure.step("add keys to admin"):
            for i, public_key in enumerate(public_keys_list):
                system.aaa.user.user_id['admin'].ssh.authorized_key.key_id[f'new_key_{i}'].set(op_param_name='key',
                                                                                               op_param_value=public_key).verify_result()
                system.aaa.user.user_id['admin'].ssh.authorized_key.key_id[f'new_key_{i}'].set(op_param_name='type',
                                                                                               op_param_value=keys_list[
                                                                                                   i % 5]).verify_result()
            with allure.step("Applying all keys configurations"):
                NvueGeneralCli.apply_config(engines.dut)

        with allure.step("connect using every single key and check connection time"):
            with PKAAuthVerifier(username="admin", private_key_path=None, hostname=engines.dut.ip, engines=engines) as admin_session_obj:
                for idx, private in enumerate(private_keys_paths_list):
                    start_time = time.time()
                    admin_session_obj.private_key_path = private
                    admin_session_obj.verify_authentication(True)
                    end_time = time.time()
                    duration = end_time - start_time
                    if duration > threshold:
                        bad_connection_timing.append({
                            'iteration': idx,
                            'duration': duration
                        })
                    with allure.independent_step(f'logged in after {duration} seconds'):
                        logger.info(f'it took {duration} seconds to log in during iteration {idx}')

        if bad_connection_timing:
            err_msg = ""
            for connection in bad_connection_timing:
                err_msg += f"Iteration {connection['iteration']}: Duration = {connection['duration']} seconds\n"

        assert not bad_connection_timing, err_msg
    finally:
        with allure.step("unset all keys"):
            system.aaa.user.user_id['admin'].ssh.unset(apply=True)
        with allure.step("delete keys for admin"):
            for private_key in private_keys_paths_list:
                SecuritySshTool.rm_auth_keypair(f"{keys_path}/{private_key}")


@pytest.mark.security
def test_ssh_pka_only(engines, topology_obj):
    """
    Verify we can not log in using password id pka in enabled

    1. run nv show system ssh-server command and verify pka-only default value is disabled
    2. add two new users - save as user_with_key, user_without_key
    3. add key only for user_with_key
    4. run nv set system ssh-server pka-only enabled + apply(with all previous configurations)
    5. run nv show system ssh-server command and verify pka-only is enabled
    6. verify we can not connect using user_with_key:password
    7. verify we can connect using user_without_key:password
    8. add new user with key and without password - this should fail - save user as user_after_enabling
    9. add password and apply
    10. try to connect with password - should fail
    11. try to connect with key - should pass
    12. run nv unset system ssh-server pka-only + apply
    13. run nv show system ssh-server command and verify pka-only is disabled
    14. try to connect with password for user_with_key and user_after_enabling - should pass
    """
    with allure.step("test PKA-only"):
        system = System()
        with allure.independent_step("validate default value"):
            show_ssh_server_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.ssh_server.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(output_dictionary=show_ssh_server_output,
                                                        field_name=SystemConsts.SSH_CONFIG_PKA_ONLY,
                                                        expected_value='disabled')

        with allure.step("create new users"):
            user_with_key, user_with_key_password = system.aaa.user.set_new_user()
            user_without_key, user_without_key_password = system.aaa.user.set_new_user()

        with allure.step(f"generate new key for {user_with_key}"):
            new_user_key, new_user_key_type, new_user_private_key_path = _generate_new_key(engines.dut, user_with_key)

        try:
            with allure.step(f"add key to {user_with_key}"):
                system.aaa.user.user_id[user_with_key].ssh.authorized_key.key_id['new_key'].set(op_param_name='key',
                                                                                                op_param_value=new_user_key).verify_result()
                system.aaa.user.user_id[user_with_key].ssh.authorized_key.key_id['new_key'].set(op_param_name='type',
                                                                                                op_param_value=new_user_key_type).verify_result()

            with allure.step(f"configure {SystemConsts.SSH_CONFIG_PKA_ONLY} to enabled"):
                system.ssh_server.set(op_param_name=SystemConsts.SSH_CONFIG_PKA_ONLY, op_param_value='enabled',
                                      apply=True)

            with allure.step("validate show command value"):
                show_ssh_server_output = OutputParsingTool.parse_json_str_to_dictionary(
                    system.ssh_server.show()).get_returned_value()
                ValidationTool.verify_field_value_in_output(output_dictionary=show_ssh_server_output,
                                                            field_name=SystemConsts.SSH_CONFIG_PKA_ONLY,
                                                            expected_value='enabled')

            with allure.step(f"verify connection with password for two user {user_without_key}, {user_with_key}"):
                with allure.independent_step(f"verify we can connect using password for {user_without_key}"):
                    with SshAuthVerifier(username=user_without_key, password=user_without_key_password, engines=engines, topology_obj=topology_obj) as ssh_connection:
                        ssh_connection.verify_authentication(True)

                with allure.independent_step(f"verify we can not connect using password for {user_with_key}"):
                    with SshAuthVerifier(username=user_with_key, password=user_with_key_password, engines=engines, topology_obj=topology_obj) as ssh_connection:
                        ssh_connection.verify_authentication(False)

            with allure.step("try to add new user with key and without password - should fail"):
                new_user = 'Test_PKA'
                new_password = 'Test123!'
                system.aaa.user.user_id[new_user].ssh.authorized_key.key_id['new_key'].set(op_param_name='key',
                                                                                           op_param_value=new_user_key).verify_result()
                system.aaa.user.user_id[new_user].ssh.authorized_key.key_id['new_key'].set(op_param_name='type',
                                                                                           op_param_value=new_user_key_type,
                                                                                           apply=True).verify_result(
                    False)

            with allure.step("add password"):
                system.aaa.user.user_id[new_user].set('password', new_password, dut_engine=engines.dut,
                                                      apply=True).verify_result()

            with allure.independent_step(f"verify we can not connect using password for {new_user}"):
                with SshAuthVerifier(username=new_user, password=new_password, engines=engines, topology_obj=topology_obj) as ssh_connection:
                    ssh_connection.verify_authentication(False)

            with allure.step(f"configure {SystemConsts.SSH_CONFIG_PKA_ONLY} to disabled"):
                system.ssh_server.unset(op_param=SystemConsts.SSH_CONFIG_PKA_ONLY, apply=True)

            with allure.step("validate show command value"):
                show_ssh_server_output = OutputParsingTool.parse_json_str_to_dictionary(
                    system.ssh_server.show()).get_returned_value()
                ValidationTool.verify_field_value_in_output(output_dictionary=show_ssh_server_output,
                                                            field_name=SystemConsts.SSH_CONFIG_PKA_ONLY,
                                                            expected_value='disabled')

            with allure.independent_step(f"verify we can connect using password for {new_user}"):
                with SshAuthVerifier(username=new_user, password=new_password, engines=engines, topology_obj=topology_obj) as ssh_connection:
                    ssh_connection.verify_authentication(True)

        finally:
            with allure.step(f"delete keys for {user_with_key}"):
                SecuritySshTool.rm_auth_keypair(f"{keys_path}/{user_with_key}")


def ssh_pka_factory_reset_no_params_check():
    """
    Verify that user keys deleted after factory reset

    1.	generate new key
    2.	add new key for admin
    3.	factory reset
    4.	verify admin can not connect using pka
    5. verify show command is empty
    """

    engines = TestToolkit.engines

    with allure.step("create system"):
        system = System()

    admin_key, admin_key_type, admin_private_key_path = _generate_new_key(engines.dut, 'admin', 'ssh-rsa')

    with allure.step("generate valid key id"):
        random_key_id = system.aaa.user.generate_username()
        admin_key_obj = system.aaa.user.user_id['admin'].ssh.authorized_key.key_id[random_key_id]

        with allure.independent_step("add new public key using three set commands to admin user"):
            admin_key_obj.set()
            admin_key_obj.set(op_param_name='key', op_param_value=admin_key)
            admin_key_obj.set(op_param_name='type', op_param_value=admin_key_type, apply=True).verify_result()

    yield  # factory reset

    with allure.step("verify that we can not connect using the key after default system factory reset"):
        with allure.step("verify we can not connect using key"):
            with PKAAuthVerifier(username='admin', private_key_path=admin_private_key_path, hostname=engines.dut.ip, engines=engines) as pka_connection:
                pka_connection.verify_authentication(False)

        with allure.step("verify show command"):
            system.aaa.user.user_id['admin'].ssh.authorized_key.key_id[random_key_id].show(should_succeed=False)

    with allure.step("delete keys for admin"):
        admin_key_obj.unset(apply=True)

    yield


def ssh_pka_factory_reset_keep_basic_check():
    """
    Verify that user keys not deleted after factory reset keep basic

    1.	generate new key
    2.	add new key for admin
    3.	factory reset keep basic
    4.	verify admin can connect using pka
    5. verify show command is not empty
    """

    engines = TestToolkit.engines

    with allure.step("create system"):
        system = System()

    admin_key, admin_key_type, admin_private_key_path = _generate_new_key(engines.dut, 'admin', 'ssh-rsa')

    with allure.step("generate valid key id"):
        random_key_id = system.aaa.user.generate_username()
        admin_key_obj = system.aaa.user.user_id['admin'].ssh.authorized_key.key_id[random_key_id]

        with allure.independent_step("add new public key using three set commands to admin user"):
            admin_key_obj.set()
            admin_key_obj.set(op_param_name='key', op_param_value=admin_key)
            admin_key_obj.set(op_param_name='type', op_param_value=admin_key_type, apply=True).verify_result()

    yield  # factory reset keep basic

    with allure.step("verify we can connect using key"):
        with PKAAuthVerifier(username='admin', private_key_path=admin_private_key_path, hostname=engines.dut.ip, engines=engines) as pka_connection:
            pka_connection.verify_authentication(True)

    with allure.step("verify show command"):
        system.aaa.user.user_id['admin'].ssh.authorized_key.key_id[random_key_id].show(should_succeed=True)

    with allure.step("delete keys for admin"):
        admin_key_obj.unset(apply=True)

    yield
