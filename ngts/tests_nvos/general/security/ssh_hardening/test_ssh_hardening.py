import pytest

from ngts.nvos_tools.infra.PexpectTool import PexpectTool
from ngts.tests_nvos.general.security.constants import SSN_OPTIONS
from ngts.tests_nvos.general.security.ssh_hardening.ssh_hardening_test_utils import *


cumulus_owner = "yleibovichwe"


@pytest.mark.cumulus
@pytest.mark.security
def test_ssh_protocol(engines):
    """
    @summary: verify the change of Protocol in sshd_config

        Steps:
        1. Verify that the new configuration is set
    """
    verify_switch_ssh_property(
        engines=engines,
        property_name=SshHardeningConsts.PROTOCOL,
        expected_value=SshHardeningConsts.VALUES[SshHardeningConsts.PROTOCOL],
        value_extraction_function=get_ssh_server_protocol
    )


@pytest.mark.cumulus
@pytest.mark.security
def test_ssh_compression(engines):
    """
    @summary: verify the change of Compression in sshd_config

        Steps:
        1. Verify that the new configuration is set
    """
    verify_switch_ssh_property(
        engines=engines,
        property_name=SshHardeningConsts.COMPRESSION,
        expected_value=SshHardeningConsts.VALUES[SshHardeningConsts.COMPRESSION],
        value_extraction_function=get_ssh_server_compression_state
    )


@pytest.mark.cumulus
@pytest.mark.security
def test_ssh_ciphers(engines, devices):
    """
    @summary: verify the change of Ciphers in sshd_config

        Steps:
        1. Verify that the new configuration is set
        2. good flow: ssh the switch with valid cipher - expect success
        3. bad flow: ssh the switch with invalid cipher - expect fail
    """
    verify_switch_ssh_property(
        engines=engines,
        property_name=SshHardeningConsts.CIPHERS,
        expected_value=get_device_ciphers_list(devices),
        value_extraction_function=get_ssh_server_ciphers
    )

    verify_ssh_with_option(
        engines=engines,
        devices=devices,
        good_flow=True,
        option_to_check=SshHardeningConsts.CIPHERS,
        get_option_list_function=get_device_ciphers_list
    )

    verify_ssh_with_option(
        engines=engines,
        devices=devices,
        good_flow=False,
        option_to_check=SshHardeningConsts.CIPHERS,
        get_option_list_function=get_device_ciphers_list
    )


@pytest.mark.cumulus
@pytest.mark.security
def test_ssh_macs(engines, devices):
    """
    @summary: verify the change of MACs in sshd_config

        Steps:
        1. Verify that the new configuration is set
        2. good flow: ssh the switch with valid MAC - expect success
        3. bad flow: ssh the switch with invalid MAC - expect fail
    """
    verify_switch_ssh_property(
        engines=engines,
        property_name=SshHardeningConsts.MACS,
        expected_value=get_device_macs_list(devices),
        value_extraction_function=get_ssh_server_macs
    )

    verify_ssh_with_option(
        engines=engines,
        devices=devices,
        good_flow=True,
        option_to_check=SshHardeningConsts.MACS,
        get_option_list_function=get_device_macs_list
    )

    verify_ssh_with_option(
        engines=engines,
        devices=devices,
        good_flow=False,
        option_to_check=SshHardeningConsts.MACS,
        get_option_list_function=get_device_macs_list
    )


@pytest.mark.cumulus
@pytest.mark.security
def test_ssh_kex_algorithms(engines, devices):
    """
    @summary: verify the change of KexAlgorithms in sshd_config

        Steps:
        1. Verify that the new configuration is set
        2. good flow: ssh the switch with valid KEX-algorithm - expect success
        3. bad flow: ssh the switch with invalid KEX-algorithm - expect fail
    """
    verify_switch_ssh_property(
        engines=engines,
        property_name=SshHardeningConsts.KEX_ALGOS,
        expected_value=get_device_kex_algotithms_list(devices),
        value_extraction_function=get_ssh_server_kex_algorithms
    )

    verify_ssh_with_option(
        engines=engines,
        devices=devices,
        good_flow=True,
        option_to_check=SshHardeningConsts.KEX_ALGOS,
        get_option_list_function=get_device_kex_algotithms_list
    )

    verify_ssh_with_option(
        engines=engines,
        devices=devices,
        good_flow=False,
        option_to_check=SshHardeningConsts.KEX_ALGOS,
        get_option_list_function=get_device_kex_algotithms_list
    )


@pytest.mark.cumulus
@pytest.mark.security
def test_ssh_auth_public_key_types(engines, upload_test_auth_keys_to_ssh_server):
    """
    @summary: verify the change of PubkeyAcceptedKeyTypes in sshd_config

        Steps:
        1. good flow: ssh the switch with key of valid type - expect success
        2. bad flow: ssh the switch with key of invalid type - expect fail
    """
    with allure.step('Good flow: ssh the switch with valid auth key. Expect success'):
        pexpect = PexpectTool(
            spawn_cmd=f'ssh {SSN_OPTIONS} '
            f'-i {SshHardeningConsts.VALID_AUTH_KEY_PATH} {engines.dut.username}@{engines.dut.ip}')
        pexpect.expect(f'{engines.dut.username}@.*~', error_message='Expected login success, but failed')
        pexpect.expect('.*')
        pexpect.sendline('logout')

    with allure.step('Bad flow: ssh the switch with invalid auth key. Expect fail (enter password prompt)'):
        pexpect = PexpectTool(
            spawn_cmd=f'ssh {SSN_OPTIONS} '
            f'-i {SshHardeningConsts.INVALID_AUTH_KEY_PATH} {engines.dut.username}@{engines.dut.ip}')
        pexpect.expect('password:',
                       error_message='Login unexpectedly succeeded. Expected login fail (enter password prompt)')
