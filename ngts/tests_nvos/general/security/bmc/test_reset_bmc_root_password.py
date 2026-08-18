import logging
import random
import time

import pytest
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from ngts.nvos_constants.constants_nvos import ApiType, PlatformConsts
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.infra.NvCommand import NvCommand
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import BmcUsers
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()
BMC_DEFAULT_KNOWN_PASSWORD = 'ABYX12#14artb'
BMC_ROOT_PASS_MANUAL_CHANGED = 'dummypassword'
AUTHENTICATION_FAILED = 'Authentication to device failed'


@pytest.mark.bmc
def test_reset_bmc_password_to_default(bmc_password_test_context, random_api):
    """
    1. Reset BMC password.
    2. Login with default.
    3. Change password - try to re-login with default
    4. reset and login with default.
    5. restore to original
    """

    # LA ignores some BMC errors during this test, pending on FR 4210208
    platform = bmc_password_test_context.platform
    client = bmc_password_test_context.client
    bmc_ip_address = bmc_password_test_context.bmc_ip_address
    root_pass = BmcUsers.root.another_password
    with allure.step("Log into bmc root with default known password"):
        bmc_engine = LinuxSshEngine(ip=bmc_ip_address, username='root', password=root_pass)
        output = bmc_engine.run_cmd('whoami')
        output = client.change_root_password(
            password=root_pass,
            new_password=BMC_ROOT_PASS_MANUAL_CHANGED,
            validate=True,
        )
        assert '"error"' not in output, (
            f'BMC curl change_root_password failed:\n{output}'
        )
        root_pass = BMC_ROOT_PASS_MANUAL_CHANGED
    with allure.step("Reset BMC Root password"):
        iterations_number = random.randint(1, 20)
        with allure.step(f"Resetting root password {iterations_number} times"):
            for _ in range(iterations_number):
                platform.bmc_password.action_reset().verify_result()
                root_pass = PlatformConsts.BMC_DEFAULT_ROOT_PASSWORD_AFTER_RESET_VIA_NOS
                bmc_engine = LinuxSshEngine(ip=bmc_ip_address, username='root', password=root_pass)
                output = bmc_engine.run_cmd('whoami')
    with allure.step("login with old bmc password, expected to fail"):
        try_to_connect_expecting_failure(
            bmc_ip_address,
            BmcUsers.root.another_password,
            err_msg='Connection with the old BMC password was expected to fail, but it passed!',
        )


@pytest.mark.bmc
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_reset_bmc_password_to_default_while_locked_out(bmc_password_test_context, test_api):
    '''
    Run the reset command after being temporarily locked out from the BMC due to too many failed connection attempts
    to the root user.
    Expected: The password will reset, and after the temporary block ends you will be able to connect with '0penBmc'.
    '''
    # LA ignores some BMC errors during this test, pending on FR 4210208
    platform = bmc_password_test_context.platform
    bmc_ip_address = bmc_password_test_context.bmc_ip_address
    lockout_duration = bmc_password_test_context.lockout_duration
    root_pass = BmcUsers.root.another_password
    with allure.step("Log into BMC root with the wrong password until the account is locked out"):
        wrong_pass = 'some#wrong#pass'
        for _ in range(3):
            logger.info("Trying to connect with wrong credentials")
            try_to_connect_expecting_failure(
                bmc_ip_address,
                wrong_pass,
                err_msg=f'Connection succeeded with wrong BMC password: {wrong_pass}',
            )

    with allure.step("Reset BMC Root password while locked out"):
        platform.bmc_password.action_reset()
        root_pass = PlatformConsts.BMC_DEFAULT_ROOT_PASSWORD_AFTER_RESET_VIA_NOS
        with allure.step(f"Wait for account lockout to expire ({lockout_duration}s)"):
            time.sleep(lockout_duration)
        bmc_engine = LinuxSshEngine(ip=bmc_ip_address, username='root', password=root_pass)
        bmc_engine.run_cmd('whoami')
    with allure.step("login with old bmc password, expected to fail"):
        try_to_connect_expecting_failure(
            bmc_ip_address,
            BmcUsers.root.another_password,
            err_msg=f'Connection succeeded with wrong BMC password: {BmcUsers.root.another_password}',
        )


@pytest.mark.disable_loganalyzer
@pytest.mark.bmc
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_reset_bmc_root_password_while_bmc_down(bmc_password_test_context, devices, test_api, nv_command: NvCommand):
    """
    1. factory reset the BMC
    2. verify bmc admin can login only with default password
    3. run show cmd - triggers bmc user password initialization via TPM
    4. verify bmc admin can login only with TPM password
    """

    # LA ignores some BMC errors during this test, pending on FR 4210208
    dut = bmc_password_test_context.dut
    platform = bmc_password_test_context.platform
    client = bmc_password_test_context.client
    bmc_ip_address = bmc_password_test_context.bmc_ip_address
    with allure.step('factory reset the bmc'):
        client.reset_bmc_to_factory()

        with allure.step("Wait for BMC to go down after factory reset"):
            client.wait_for_bmc_unavailable()

        with allure.step("Try To reset bmc root password while BMC is down"):
            output = platform.bmc_password.action_reset().verify_result(should_succeed=False)
            failed_to_connect_err = "Failed to reset password: Can't connect to BMC"
            assert failed_to_connect_err in output, (
                f'Output expected to contain {failed_to_connect_err}, but instead got {output}'
            )

        with allure.step("Wait for BMC to boot after factory reset"):
            client.wait_for_bmc_available(username=BmcUsers.root.username,
                                          password=BmcUsers.root.default_password)
        with allure.step("Wait for CPU to see that BMC has booted"):
            BmcTool.wait_for_cpu_to_detect_bmc(dut, nv_command)

        with allure.step("Reset BMC Password to default - using nvos command"):
            output = platform.bmc_password.action_reset().verify_result()
            root_password = PlatformConsts.BMC_DEFAULT_ROOT_PASSWORD_AFTER_RESET_VIA_NOS
            logger.info("Verify login is available with default bmc password for reset bmc root password")
            bmc_engine = LinuxSshEngine(ip=bmc_ip_address, username='root', password=root_password)
            output = bmc_engine.run_cmd('whoami')


def try_to_connect_expecting_failure(bmc_ip_address, password, err_msg):
    try:
        bmc_engine = LinuxSshEngine(ip=bmc_ip_address, username='root', password=password)
        # Bypass the engine's connection retry loop so one negative check does not lock the BMC account.
        connection = bmc_engine.create_engine()
    except Exception as error:
        assert AUTHENTICATION_FAILED in str(error), (
            f'Expected an authentication failure containing {AUTHENTICATION_FAILED!r}, got: {error}'
        )
        return

    connection.disconnect()
    pytest.fail(err_msg)
