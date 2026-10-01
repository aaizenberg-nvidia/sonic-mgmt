from ngts.tests_nvos.general.security.ssh_ciphers.ssh_ciphers_utilis import boundary_check, clean_ssh_server_configuration, cleanup_after_test, extract_negotiated_algorithms, general_test_flow, generate_user, get_ssh_verbose_output, pubkey_test_flow, set_ssh_server_param, setup_api_type, verify_values_for_property
import pytest
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.ssh_ciphers.constants import SshCiphersConsts
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
import time
from ngts.tests_nvos.general.security.ssh_ciphers.SshKeyManager import SshKeyManager
import logging
from ngts.cli_wrappers.common.general_clis_common import GeneralCliCommon


cumulus_owner = "yleibovichwe"


@pytest.mark.cumulus
@pytest.mark.security
@pytest.mark.parametrize("api_type", [ApiType.NVUE, ApiType.OPENAPI])
def test_ssh_hardening(engines, api_type):
    """
    @summary: verify the change of Ciphers, MACs, KexAlgorithms, HostKeyAlgorithms, PubkeyAcceptedKeyTypes in sshd_config

        Steps:
        1. Verify that default values of ciphers, macs, kex-algorithms, host-key-algorithms, pubkey-accepted-key-types are set
        2. verify compression, x11forwarding and allowtcpforwarding is not
        3. verify old strict field is not present and nv set system ssh-server strict enabled/disabled is failed
    """
    original_api = setup_api_type(engines.dut, api_type)
    try:
        ssh_server = System().ssh_server

        with allure.step(f'unset system ssh-server ciphers, macs, kex-algorithms, host-key-algorithms, pubkey-accepted-key-types to make sure default values are set'):
            clean_ssh_server_configuration(engines.dut)

        with allure.step(f'verify nv set system ssh-server strict enabled is failed'):
            ssh_server.set(op_param_name='strict', op_param_value='enabled', apply=True, ask_for_confirmation='-y').verify_result(should_succeed=False)

        with allure.step(f'verify nv set system ssh-server strict disabled is failed'):
            ssh_server.set(op_param_name='strict', op_param_value='disabled', apply=True, ask_for_confirmation='-y').verify_result(should_succeed=False)
    finally:
        TestToolkit.tested_api = original_api


@pytest.mark.cumulus
@pytest.mark.security
@pytest.mark.parametrize("api_type", [ApiType.NVUE, ApiType.OPENAPI])
def test_ssh_ciphers_configuration(engines, api_type):
    """
    @summary: verify the change of Ciphers in sshd_config

        Steps:
        for each cipher in possible values:
            1. configure system ssh-server ciphers with the cipher
            2. verify the cipher is set in sshd_config
            2. good flow: verify login via ssh with the cipher is successful
            3. bad flow: verify login via ssh with invalid cipher is failed
        cleanup:
        1. unset system ssh-server ciphers
        2. verify default values of ciphers are set
    """
    original_api = setup_api_type(engines.dut, api_type)
    try:
        general_test_flow(engines.dut, api_type, SshCiphersConsts.CIPHERS, list(SshCiphersConsts.DEFAULT_VALUES[SshCiphersConsts.CIPHERS]))
    finally:
        cleanup_after_test(engines.dut, original_api=original_api, property_name=SshCiphersConsts.CIPHERS)


@pytest.mark.cumulus
@pytest.mark.security
@pytest.mark.parametrize("api_type", [ApiType.NVUE, ApiType.OPENAPI])
def test_ssh_macs_configuration(engines, api_type):
    """
    @summary: verify the change of Macs in sshd_config

        Steps:
        for each mac in possible values:
            1. configure system ssh-server macs with the mac
            2. verify the mac is set in sshd_config
            2. good flow: verify login via ssh with the mac is successful
            3. bad flow: verify login via ssh with invalid mac is failed
        cleanup:
        1. unset system ssh-server macs
        2. verify default values of macs are set
    """
    original_api = setup_api_type(engines.dut, api_type)
    try:
        general_test_flow(engines.dut, api_type, SshCiphersConsts.MACS, list(SshCiphersConsts.DEFAULT_VALUES[SshCiphersConsts.MACS]))
    finally:
        cleanup_after_test(engines.dut, original_api=original_api, property_name=SshCiphersConsts.MACS)


@pytest.mark.cumulus
@pytest.mark.security
@pytest.mark.parametrize("api_type", [ApiType.NVUE, ApiType.OPENAPI])
def test_ssh_kex_algorithms_configuration(engines, api_type):
    """
    @summary: verify the change of KexAlgorithms in sshd_config

        Steps:
        for each kex algorithm in possible values:
            1. configure system ssh-server kex-algorithms with the kex algorithm
            2. verify the kex algorithm is set in sshd_config
            2. good flow: verify login via ssh with the kex algorithm is successful
            3. bad flow: verify login via ssh with invalid kex algorithm is failed
        cleanup:
        1. unset system ssh-server kex-algorithms
        2. verify default values of kex-algorithms are set
    """
    original_api = setup_api_type(engines.dut, api_type)
    try:
        general_test_flow(engines.dut, api_type, SshCiphersConsts.KEX_ALGOS, list(SshCiphersConsts.DEFAULT_VALUES[SshCiphersConsts.KEX_ALGOS]))
    finally:
        cleanup_after_test(engines.dut, original_api=original_api, property_name=SshCiphersConsts.KEX_ALGOS)


@pytest.mark.cumulus
@pytest.mark.security
@pytest.mark.parametrize("api_type", [ApiType.NVUE, ApiType.OPENAPI])
def test_host_key_algorithms_configuration(engines, api_type):
    """
    @summary: verify the change of HostKeyAlgorithms in sshd_config

        Steps:
        for each host key algorithm in possible values:
            1. configure system ssh-server host-key-algorithms with the host key algorithm
            2. verify the host key algorithm is set in sshd_config
            2. good flow: verify login via ssh with the host key algorithm is successful
            3. bad flow: verify login via ssh with invalid host key algorithm is failed
        cleanup:
        1. unset system ssh-server host-key-algorithms
        2. verify default values of host-key-algorithms are set
    """
    original_api = setup_api_type(engines.dut, api_type)
    try:
        general_test_flow(engines.dut, api_type, SshCiphersConsts.HOST_KEY_ALGOS, list(SshCiphersConsts.DEFAULT_VALUES[SshCiphersConsts.HOST_KEY_ALGOS]))
    finally:
        cleanup_after_test(engines.dut, original_api=original_api, property_name=SshCiphersConsts.HOST_KEY_ALGOS)


@pytest.mark.cumulus
@pytest.mark.security
@pytest.mark.parametrize("api_type", [ApiType.NVUE, ApiType.OPENAPI])
def test_ssh_pubkey_accepted_algorithms_configuration(engines, api_type):
    """
    @summary: verify the change of PubkeyAcceptedKeyTypes in sshd_config

        Steps:
        for each pubkey-accepted-key-type in possible values:
            1. configure system ssh-server pubkey-accepted-key-types with the pubkey-accepted-key-type
            2. verify the pubkey-accepted-key-type is set in sshd_config
            2. good flow: verify login via ssh with the pubkey-accepted-key-type is successful
            3. bad flow: verify login via ssh with invalid pubkey-accepted-key-type is failed
        cleanup:
        1. unset system ssh-server pubkey-accepted-key-types
        2. verify default values of pubkey-accepted-key-types are set
    """
    original_api = setup_api_type(engines.dut, api_type)
    keys_manager = SshKeyManager(engines.dut)
    username, password = generate_user()
    try:
        with allure.step(f'Generate keys'):
            for key in SshCiphersConsts.DEFAULT_VALUES[SshCiphersConsts.PUBKEY_ACCEPTED_ALGOS]:
                keys_manager.generate_key_and_upload_to_server(key)
                if 'cert' in key:
                    keys_manager.sign_certificate_key(key, username)

        with allure.step(f'enable ssh cert-auth state for user {username} and apply config'):
            System().aaa.user.user_id[username].ssh.cert_auth.set(op_param_name='state', op_param_value='enabled', apply=True, ask_for_confirmation='-y').verify_result()

        pubkey_test_flow(engines.dut, keys_manager, username, list(SshCiphersConsts.DEFAULT_VALUES[SshCiphersConsts.PUBKEY_ACCEPTED_ALGOS]))

    finally:
        keys_manager.clean_keys()
        try:
            System().aaa.user.unset(op_param=username, apply=True, ask_for_confirmation='-y').verify_result()
        except Exception as e:
            logging.warning(f'Failed to unset user {username}: {e}')
        finally:
            cleanup_after_test(engines.dut, original_api=original_api, property_name=SshCiphersConsts.PUBKEY_ACCEPTED_ALGOS)


@pytest.mark.cumulus
@pytest.mark.security
@pytest.mark.parametrize("api_type", [ApiType.NVUE, ApiType.OPENAPI])
def test_ssh_ciphers_boundaries(engines, api_type):
    """
    @summary: verify the boundaries of the ssh ciphers
    """
    boundary_check(engines.dut, api_type, property_name=SshCiphersConsts.CIPHERS)


@pytest.mark.cumulus
@pytest.mark.security
@pytest.mark.parametrize("api_type", [ApiType.NVUE, ApiType.OPENAPI])
def test_ssh_macs_boundaries(engines, api_type):
    """
    @summary: verify the boundaries of the ssh macs
    """
    boundary_check(engines.dut, property_name=SshCiphersConsts.MACS, api_type=api_type)


@pytest.mark.cumulus
@pytest.mark.security
@pytest.mark.parametrize("api_type", [ApiType.NVUE, ApiType.OPENAPI])
def test_ssh_kex_algorithms_boundaries(engines, api_type):
    """
    @summary: verify the boundaries of the ssh kex algorithms
    """
    boundary_check(engines.dut, property_name=SshCiphersConsts.KEX_ALGOS, api_type=api_type)


@pytest.mark.cumulus
@pytest.mark.security
@pytest.mark.parametrize("api_type", [ApiType.NVUE, ApiType.OPENAPI])
def test_ssh_host_key_algorithms_boundaries(engines, api_type):
    """
    @summary: verify the boundaries of the ssh host key algorithms
    """
    boundary_check(engines.dut, property_name=SshCiphersConsts.HOST_KEY_ALGOS, api_type=api_type)


@pytest.mark.cumulus
@pytest.mark.security
@pytest.mark.parametrize("api_type", [ApiType.NVUE, ApiType.OPENAPI])
def test_ssh_pubkey_accepted_algorithms_boundaries(engines, api_type):
    """
    @summary: verify the boundaries of the ssh pubkey accepted key algorithms
    """
    boundary_check(engines.dut, property_name=SshCiphersConsts.PUBKEY_ACCEPTED_ALGOS, api_type=api_type)


@pytest.mark.cumulus
@pytest.mark.security
@pytest.mark.parametrize("api_type", [ApiType.NVUE, ApiType.OPENAPI])
def test_restart_ssh_server(engines, api_type):
    """
    @summary: verify the restart of the ssh server
    Steps:
        1. configure the ssh server with the some valid values
        2. verify the configurations are set in sshd_config
        2. restart the ssh server
        3. verify the configurations are persisted after restart
        4. verify ssh is working with the same valid values that were configured
        cleanup:
        1. unset the ssh server configurations
        2. verify the default values of all ciphers, macs, kex-algorithms, host-key-algorithms, pubkey-accepted-key-types are set
    """
    original_api = setup_api_type(engines.dut, api_type)
    try:
        ssh_server = System().ssh_server
        keys_to_check = dict()
        keys_to_check[SshCiphersConsts.CIPHERS] = SshCiphersConsts.DEFAULT_VALUES[SshCiphersConsts.CIPHERS][1:]
        keys_to_check[SshCiphersConsts.MACS] = SshCiphersConsts.DEFAULT_VALUES[SshCiphersConsts.MACS][1:]
        keys_to_check[SshCiphersConsts.KEX_ALGOS] = SshCiphersConsts.DEFAULT_VALUES[SshCiphersConsts.KEX_ALGOS][:2]
        keys_to_check[SshCiphersConsts.HOST_KEY_ALGOS] = SshCiphersConsts.DEFAULT_VALUES[SshCiphersConsts.HOST_KEY_ALGOS][1:]
        keys_to_check[SshCiphersConsts.PUBKEY_ACCEPTED_ALGOS] = SshCiphersConsts.DEFAULT_VALUES[SshCiphersConsts.PUBKEY_ACCEPTED_ALGOS][1:6]

        with allure.step('configuring the ssh server'):
            for i, (key, value) in enumerate(keys_to_check.items()):
                set_ssh_server_param(op_param_name=key, value=value, apply=i == len(keys_to_check) - 1)

        with allure.step('verify configurations'):
            for key, value in keys_to_check.items():
                verify_values_for_property(engines.dut, property_name=key, expected_value=value)

        with allure.step('restart the ssh service'):
            # engines.dut.run_cmd('sudo systemctl restart ssh', validate=True)
            GeneralCliCommon(engines.dut).systemctl_restart('ssh')

        with allure.step('wait for ssh server to restart'):
            time.sleep(5)
            assert GeneralCliCommon(engines.dut).systemctl_is_service_active('ssh'), f'SSH service is not active'

        with allure.step('verify configurations persisted after restart'):
            for key, value in keys_to_check.items():
                verify_values_for_property(engines.dut, property_name=key, expected_value=value)

        with allure.step('verify ssh is working'):
            ssh_options, success = get_ssh_verbose_output(engines.dut)
            assert success, f'SSH connection failed. SSH output: {ssh_options}'

            negotiated = extract_negotiated_algorithms(ssh_options)
            for key in [SshCiphersConsts.CIPHERS, SshCiphersConsts.KEX_ALGOS, SshCiphersConsts.HOST_KEY_ALGOS]:
                assert negotiated[key] in keys_to_check[key], f'{key} {keys_to_check[key]} not in negotiated: {negotiated}'
            assert 'gcm' in negotiated[SshCiphersConsts.CIPHERS] or negotiated[SshCiphersConsts.MACS] in keys_to_check[SshCiphersConsts.MACS], f'MAC {keys_to_check[SshCiphersConsts.MACS]} not in negotiated: {negotiated}'
    finally:
        cleanup_after_test(engines.dut, original_api=original_api)
