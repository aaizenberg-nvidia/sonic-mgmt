from typing import List, Tuple

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import SystemConsts, UserRole
from ngts.nvos_tools.infra.FilesTool import FilesTool
from ngts.nvos_tools.infra.NvCommand import NvCommand
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.security_test_tools.constants import AddressingType
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tests_nvos.general.security.test_ssh_cert_auth.constants import CA_KEY_TYPE_MISMATCH_ERRS
from ngts.tests_nvos.general.security.test_ssh_cert_auth.helpers import (
    SshCertAuthHelper,
    get_random_key_type,
    get_random_principal,
    get_random_principals,
    set_cert_auth,
    set_trusted_ca_key,
    verify_user_login,
)
from ngts.tests_nvos.helpers.general_helpers import generate_rand_str
from ngts.tools.test_utils import allure_utils as allure


@pytest.mark.security
@pytest.mark.ssh
@pytest.mark.parametrize("addressing_type", [AddressingType.IPV4, AddressingType.IPV6])
def test_ssh_cert_basic_func(
    engines,
    local_admin_user: UserInfo,
    local_monitor_user: UserInfo,
    addressing_type: str,
    dut_ipv6_addr: str,
    nv_command: NvCommand,
    ssh_cert_auth_helper_with_cleanup: Tuple[SshCertAuthHelper, str],
    random_api,
):
    """
    This test performs basic functionality check of the feature.
    That user can connect without any password by providing private key.
    Should test admin/monitor user and also non-default generated users.

    Test Flow:
    1.		Generates an SSH key pair and user key pair, sign it.
    2.		Set the Principal for the User
    3.		Enable certificate-based authentication for the user
    4.		Set the trusted CA public key file (Test all supported key types)
    5.		Verify show commands
    6.		Log in using the signed certificate

    """
    system = nv_command.system
    ssh_cert_auth_helper, key_name = ssh_cert_auth_helper_with_cleanup
    key_type = get_random_key_type()
    random_principal = get_random_principal()
    admin = local_admin_user
    monitor = local_monitor_user
    users = [admin, monitor]
    hostname = engines.dut.ip if addressing_type == AddressingType.IPV4 else dut_ipv6_addr

    ca_val, key_private_path = ssh_cert_auth_helper.generate_keys_and_sign_certificate(
        key_name=key_name, key_type=key_type, principals=[random_principal]
    )

    for user in users:
        set_cert_auth(system=system, user=user, principal=random_principal, state="enabled")

    set_trusted_ca_key(system, key_name, key_type, ca_val, apply=True)
    _verify_show_commands(system, users, key_name, key_type, random_principal, expected_state="enabled")

    # Verify principals file EXISTS for both users when cert-auth is enabled
    _verify_principals_file(engines, admin.username, principal=random_principal, should_exist=True)
    _verify_principals_file(engines, monitor.username, principal=random_principal, should_exist=True)

    verify_user_login(admin, key_private_path, hostname, engines, expect_success=True)
    verify_user_login(monitor, key_private_path, hostname, engines, expect_success=True)

    with allure.step("disable cert auth for user"):
        system.aaa.user.user_id[admin.username].ssh.cert_auth.disable_state(apply=True)

    _verify_show_commands(system, [admin], key_name, key_type, expected_state="disabled")

    # Verify principals file does NOT exist for admin (disabled) but EXISTS for monitor (enabled)
    _verify_principals_file(engines, admin.username, should_exist=False)
    _verify_principals_file(engines, monitor.username, principal=random_principal, should_exist=True)

    verify_user_login(admin, key_private_path, hostname, engines, expect_success=False)
    verify_user_login(monitor, key_private_path, hostname, engines, expect_success=True)


@pytest.mark.security
@pytest.mark.ssh
def test_ssh_cert_auth_disabled(
    engines, nv_command: NvCommand, ssh_cert_auth_helper_with_cleanup: Tuple[SshCertAuthHelper, str], random_api, dut_hostname: str
):
    """
    This test performs check that user with auth disabled can’t connect to the system via key provided.

    Test Flow:
    1.		Generates an SSH key pair and user key	General generation steps should succeed
    2.		Set the Principal for the User
        nv set system aaa user {user} ssh cert-auth principals {principal}
    3.		Disable certificate-based authentication
        nv set system aaa user {user} ssh cert-auth state disabled
    4.		Set the trusted CA public key file
        nv set system ssh-server trusted-ca-keys <key-id> key <key-val>
        nv set system ssh-server trusted-ca-keys <key-id> type <key-type>
    5.		Verify show commands
        nv show system ssh-server trusted-ca-keys – Should list trusted ca
        nv show system ssh-server – Should have trusted ca key
        nv show system ssh-server trusted-ca-keys {key_id}-
        nv show system aaa user {user} ssh cert-auth

            Should list user cert-auth state as disabled
    6.		Login using the signed certificate
        Verify that user fails to login
    7.		Login using the user password
        Verify that user succeed to login
    8.		Enable certificate-based authentication
        nv set system aaa user {user} ssh cert-auth state enable
    9.		Login using the signed certificate
        Verify that user succeed to login

    """
    system = nv_command.system
    ssh_cert_auth_helper, key_name = ssh_cert_auth_helper_with_cleanup
    key_type = get_random_key_type()
    random_principal = get_random_principal()
    admin = UserInfo(SystemConsts.DEFAULT_USER_ADMIN, SystemConsts.DEFAULT_USER_ADMIN, UserRole.ADMIN)
    monitor = UserInfo(SystemConsts.DEFAULT_USER_MONITOR, SystemConsts.DEFAULT_USER_MONITOR, UserRole.MONITOR)
    users = [admin, monitor]
    hostname = dut_hostname

    ca_val, key_private_path = ssh_cert_auth_helper.generate_keys_and_sign_certificate(
        key_name=key_name, key_type=key_type, principals=[random_principal]
    )

    for user in users:
        set_cert_auth(system=system, user=user, principal=random_principal, state="disabled")

    set_trusted_ca_key(system, key_name, key_type, ca_val, apply=True)
    _verify_show_commands(system, users, key_name, key_type, expected_state="disabled")

    # Verify principals file does NOT exist when cert-auth state is disabled
    _verify_principals_file(engines, admin.username, should_exist=False)
    _verify_principals_file(engines, monitor.username, should_exist=False)

    verify_user_login(admin, key_private_path, hostname, engines, expect_success=False)
    verify_user_login(monitor, key_private_path, hostname, engines, expect_success=False)

    with allure.step("enable cert auth for user"):
        system.aaa.user.user_id[admin.username].ssh.cert_auth.enable_state(apply=True)

    _verify_show_commands(system, [admin], key_name, key_type, random_principal, expected_state="enabled")
    _verify_show_commands(system, [monitor], key_name, key_type, expected_state="disabled")

    # Verify principals file EXISTS for admin (enabled) but NOT for monitor (disabled)
    _verify_principals_file(engines, admin.username, principal=random_principal, should_exist=True)
    _verify_principals_file(engines, monitor.username, should_exist=False)

    verify_user_login(admin, key_private_path, hostname, engines, expect_success=True)
    verify_user_login(monitor, key_private_path, hostname, engines, expect_success=False)


@pytest.mark.security
@pytest.mark.ssh
def test_ssh_cert_auth_multiple_principals(
    engines,
    local_admin_user: UserInfo,
    nv_command: NvCommand,
    ssh_cert_auth_helper_with_cleanup: Tuple[SshCertAuthHelper, str],
    random_api,
    dut_hostname: str,
):
    """
    This test performs check that user can connect to the system via key provided if it has multiple principals.
    Test Flow:
    1.		Generates an SSH key pair and user key
    2.		Set the Principal for the User
        nv set system aaa user {user} ssh cert-auth principals {principal}
        nv set system aaa user {local_admin_user} ssh cert-auth principals {another_principal}
    3.		Set the trusted CA public key file
        nv set system ssh-server trusted-ca-keys <key-id> key <key-val>
        nv set system ssh-server trusted-ca-keys <key-id> type <key-type>
    4.		Verify show commands
        nv show system ssh-server trusted-ca-keys – Should list trusted ca
        nv show system ssh-server – Should have trusted ca key
        nv show system ssh-server trusted-ca-keys {key_id}-
        Should show the specified id for the key
        nv show system aaa user {user} ssh cert-auth
        -	Should list user cert-auth state as enabled
    5.		Login using the signed certificate
        Verify that user succeed to login
        Verify that another user succeed to login with another principal
    """
    system = nv_command.system
    ssh_cert_auth_helper, key_name = ssh_cert_auth_helper_with_cleanup
    key_type = get_random_key_type()
    random_principals = get_random_principals(number_of_values_to_select=2)
    random_principal = random_principals[0]
    another_random_principal = random_principals[1]
    admin = UserInfo(SystemConsts.DEFAULT_USER_ADMIN, SystemConsts.DEFAULT_USER_ADMIN, UserRole.ADMIN)
    hostname = dut_hostname

    ca_val, key_private_path = ssh_cert_auth_helper.generate_keys_and_sign_certificate(
        key_name=key_name, key_type=key_type, principals=[random_principal, another_random_principal]
    )

    set_cert_auth(system=system, user=admin, principal=random_principal, state="enabled")
    set_cert_auth(system=system, user=local_admin_user, principal=another_random_principal, state="enabled")

    set_trusted_ca_key(system, key_name, key_type, ca_val, apply=True)

    _verify_show_commands(system, [admin], key_name, key_type, random_principal, expected_state="enabled")
    _verify_show_commands(system, [local_admin_user], key_name, key_type, another_random_principal, expected_state="enabled")

    # Verify principals files exist with correct principals
    _verify_principals_file(engines, admin.username, principal=random_principal, should_exist=True)
    _verify_principals_file(engines, local_admin_user.username, principal=another_random_principal, should_exist=True)

    verify_user_login(admin, key_private_path, hostname, engines, expect_success=True)
    verify_user_login(local_admin_user, key_private_path, hostname, engines, expect_success=True)


@pytest.mark.security
@pytest.mark.ssh
def test_ssh_cert_auth_multiple_keys(
    engines,
    local_admin_user: UserInfo,
    nv_command: NvCommand,
    ssh_cert_auth_helper_with_cleanup: Tuple[SshCertAuthHelper, str],
    random_api,
    dut_hostname: str,
):
    """
    This test performs check that user can connect to the system via key provided if it has multiple keys.
    Test Flow:
    1.		Generates an SSH key pair and user key
    2.      Generate another key pair
    3.		Set the Principal for the User
        nv set system aaa user {user} ssh cert-auth principals {principal}
    4.		Set the trusted CA public key file
        nv set system ssh-server trusted-ca-keys <key-id> key <key-val>
        nv set system ssh-server trusted-ca-keys <key-id> type <key-type>
    4.		Verify show commands
        nv show system ssh-server trusted-ca-keys – Should list trusted ca
        nv show system ssh-server – Should have trusted ca key
        nv show system ssh-server trusted-ca-keys {key_id}-
    Should show the specified id for the key
    5.		Login using the signed certificate
        Verify that user succeed to login with the first key
        Verify that user succeed to login with the second key
    """
    system = nv_command.system
    ssh_cert_auth_helper, key_name = ssh_cert_auth_helper_with_cleanup
    another_key_name = generate_rand_str(str_len=10)
    key_type = get_random_key_type()
    random_principal = get_random_principal()
    admin = UserInfo(SystemConsts.DEFAULT_USER_ADMIN, SystemConsts.DEFAULT_USER_ADMIN, UserRole.ADMIN)
    hostname = dut_hostname

    _, _, key_path = ssh_cert_auth_helper.generate_user_key_pair(key_name, key_type)
    _, _, another_key_path = ssh_cert_auth_helper.generate_user_key_pair(another_key_name, key_type)
    ca_val, key_type, _ = ssh_cert_auth_helper.generate_ca_key_pair(key_name, key_type)
    ssh_cert_auth_helper.sign_user_certificate(f"{key_name}_ca", f"{key_name}_key", principals=[random_principal])
    ssh_cert_auth_helper.sign_user_certificate(f"{key_name}_ca", f"{another_key_name}_key", principals=[random_principal])

    set_cert_auth(system=system, user=admin, principal=random_principal, state="enabled")

    set_trusted_ca_key(system, key_name, key_type, ca_val, apply=True)

    _verify_show_commands(system, [admin], key_name, key_type, random_principal, expected_state="enabled")

    # Verify principals file exists with correct principal
    _verify_principals_file(engines, admin.username, principal=random_principal, should_exist=True)

    verify_user_login(admin, key_path, hostname, engines, expect_success=True)
    verify_user_login(admin, another_key_path, hostname, engines, expect_success=True)


@pytest.mark.security
@pytest.mark.ssh
def test_bad_principal(
    engines,
    local_admin_user: UserInfo,
    nv_command: NvCommand,
    ssh_cert_auth_helper_with_cleanup: Tuple[SshCertAuthHelper, str],
    random_api,
    dut_hostname: str,
):
    """
    This test performs check that user can't connect to the system via key provided if the principal is not matching.
    Test Flow:
        1. Generates an SSH key pair and user key
        2. Set bad principal for the user
        3. Set the trusted CA public key file
        4. Verify show commands
        5. Login using the signed certificate
        6. Verify that user fails to login
    """
    system = nv_command.system
    ssh_cert_auth_helper, key_name = ssh_cert_auth_helper_with_cleanup
    key_type = get_random_key_type()
    random_principal = get_random_principal()
    bad_principal = "bad_principal"
    admin = local_admin_user
    hostname = dut_hostname

    _, _, key_path = ssh_cert_auth_helper.generate_user_key_pair(key_name, key_type)
    ca_val, key_type, _ = ssh_cert_auth_helper.generate_ca_key_pair(key_name, key_type)
    ssh_cert_auth_helper.sign_user_certificate(f"{key_name}_ca", f"{key_name}_key", principals=[random_principal])

    set_cert_auth(system=system, user=admin, principal=bad_principal, state="enabled")
    set_trusted_ca_key(system, key_name, key_type, ca_val, apply=True)
    _verify_show_commands(system, [admin], key_name, key_type, bad_principal, expected_state="enabled")

    # Verify principals file exists but with wrong principal (bad_principal instead of random_principal)
    _verify_principals_file(engines, admin.username, principal=bad_principal, should_exist=True)

    verify_user_login(admin, key_path, hostname, engines, expect_success=False)


@pytest.mark.security
@pytest.mark.ssh
def test_bad_key_type(
    engines,
    local_admin_user: UserInfo,
    nv_command: NvCommand,
    ssh_cert_auth_helper_with_cleanup: Tuple[SshCertAuthHelper, str],
    random_api,
    dut_hostname: str,
):
    """
    Verify apply rejects a trusted CA key type that does not match the key material,
    and that the matching CA and cert-auth remain.

    1. Enable cert-auth and apply a matching trusted CA key type
    2. Verify show and login succeed
    3. Apply a mismatched CA type — expect reject
    4. Verify the matching CA is still in show and login still succeeds
    """
    system = nv_command.system
    ssh_cert_auth_helper, key_name = ssh_cert_auth_helper_with_cleanup
    key_type = get_random_key_type()
    random_principal = get_random_principal()
    bad_key_type = get_random_key_type(exclude=[key_type])
    admin = local_admin_user
    hostname = dut_hostname

    _, _, key_path = ssh_cert_auth_helper.generate_user_key_pair(key_name, key_type)
    ca_val, key_type, _ = ssh_cert_auth_helper.generate_ca_key_pair(key_name, key_type)
    ssh_cert_auth_helper.sign_user_certificate(f"{key_name}_ca", f"{key_name}_key", principals=[random_principal])

    set_cert_auth(system=system, user=admin, principal=random_principal, state="enabled")
    with allure.step(f"apply matching trusted CA key type {key_type}"):
        set_trusted_ca_key(system, key_name, key_type, ca_val, apply=True).verify_result()
    _verify_show_commands(system, [admin], key_name, key_type, random_principal, expected_state="enabled")
    _verify_principals_file(engines, admin.username, principal=random_principal, should_exist=True)
    verify_user_login(admin, key_path, hostname, engines, expect_success=True)

    with allure.step(f"apply CA type {bad_key_type} that does not match key material - expect reject"):
        set_trusted_ca_key(system, key_name, bad_key_type, ca_val, apply=True).verify_result(
            should_succeed=False, expected_value=CA_KEY_TYPE_MISMATCH_ERRS)
        # The rejected value stays staged in the pending revision - discard it so it
        # does not get re-applied by the next apply in the session.
        NvueGeneralCli.detach_config(engines.dut)
    _verify_show_commands(system, [admin], key_name, key_type, random_principal, expected_state="enabled")
    verify_user_login(admin, key_path, hostname, engines, expect_success=True)


@pytest.mark.security
@pytest.mark.ssh
def test_principals_file_cleanup_on_user_deletion(
    engines,
    local_monitor_user: UserInfo,
    nv_command: NvCommand,
    ssh_cert_auth_helper_with_cleanup: Tuple[SshCertAuthHelper, str],
    random_api,
):
    """
    Test that principals file is removed when user is deleted.

    Related to bug #4879586: [ssh-trusted-keys]: Default Principals are not removed
    as result of deleting users with cert-auth enabled

    Test Flow:
    1. Use existing local_monitor_user with cert-auth enabled and principals configured
    2. Verify principals file exists in /etc/ssh/principals/
    3. Delete the user via 'nv unset system aaa user'
    4. Verify principals file is removed from /etc/ssh/principals/
    """
    system = nv_command.system
    ssh_cert_auth_helper, key_name = ssh_cert_auth_helper_with_cleanup
    key_type = get_random_key_type()
    random_principal = get_random_principal()

    # Step 1: Configure cert-auth for local_monitor_user
    ca_val, _ = ssh_cert_auth_helper.generate_keys_and_sign_certificate(
        key_name=key_name, key_type=key_type, principals=[random_principal]
    )

    set_cert_auth(system=system, user=local_monitor_user, principal=random_principal, state="enabled")
    set_trusted_ca_key(system, key_name, key_type, ca_val, apply=True)

    # Step 2: Verify principals file EXISTS
    _verify_principals_file(engines, local_monitor_user.username, principal=random_principal, should_exist=True)

    # Step 3: Delete the user
    system.aaa.user.user_id[local_monitor_user.username].unset(apply=True)

    # Step 4: Verify principals file is REMOVED
    _verify_principals_file(engines, local_monitor_user.username, should_exist=False)


def _verify_show_commands(
    system: System,
    users: List[UserInfo],
    key_name: str,
    key_type: str,
    random_principal: str = "",
    expected_state: str = "",
):
    with allure.step("verify show commands"):
        _verify_trusted_ca_key(system, key_name, key_type)
        _verify_users_principals_and_state(system, users, random_principal, expected_state)


def _verify_trusted_ca_key(system: System, key_name: str, key_type: str):
    """
    Verify the trusted CA key is set as expected.
    """
    with allure.independent_step(f"verify trusted ca key for {key_name}"):
        out = system.ssh_server.trusted_ca_keys.key_id[key_name].parse_show()
        assert out.get("key", "") == "*", f"trusted ca key val is not set as expected. actual: {out}"
        assert out.get("type", "") == key_type, f"trusted ca key type is not set as expected. actual: {out}"


def _verify_users_principals_and_state(system: System, users: List[UserInfo], random_principal: str, expected_state: str):
    """
    Verify the users principals and state are set as expected.
    """
    for user in users:
        with allure.independent_step(f"verify user {user.username} role {user.role} principals and state {expected_state}"):
            out = system.aaa.user.user_id[user.username].ssh.cert_auth.parse_show()
            out_principals = out.get("principals", "")
            out_state = out.get("state", "")
            if random_principal:
                assert random_principal in out_principals, f"principals for user {user.username} are not set as expected. actual: {out}"
            else:
                assert not out_principals, f"principals for user {user.username} are not set as expected. actual: {out}"
            assert out_state == expected_state, f"user {user.username} state is not set as expected. actual: {out}"


def _verify_principals_file(engines, username: str, principal: str | None = None, should_exist: bool = True) -> None:
    """
    Verify the principals file in /etc/ssh/principals/{username} exists and contains expected principal.

    Uses FilesTool with ResultObj pattern for clean verification.

    Args:
        engines: Test engines
        username: Username to check principals file for
        principal: Expected principal in the file (if None, just check existence)
        should_exist: Whether the file should exist or not

    Raises:
        AssertionError: If file existence doesn't match expectation or content verification fails
    """
    principals_file_path = f"/etc/ssh/principals/{username}"

    with allure.step(f"verify principals file for user {username}, should_exist={should_exist}"):
        # Verify file existence using ResultObj pattern with should_succeed parameter
        FilesTool.file_exists_sudo(engines.dut, principals_file_path).verify_result(should_succeed=should_exist)

        # If file should exist and principal is specified, verify content
        if should_exist and principal:
            # Read content and verify it contains the expected principal
            # If file can't be read, error message becomes content and verification fails
            FilesTool.read_file_content(
                engines.dut,
                principals_file_path,
                use_sudo=True
            ).verify_result(expected_value=principal)
