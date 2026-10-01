import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.validations.traffic_validations.ping.send import ping_till_alive
from retry import retry

from ngts.nvos_constants.constants_nvos import PlatformConsts
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.infra.CurlTool import CurlTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import BmcUsers
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)

REDFISH_AUTH_ERROR = 'Invalid username or password'
BMC_LOCKOUT_DURATION_FALLBACK = 10 * 60


@dataclass(frozen=True, slots=True)
class BmcPasswordTestContext:
    dut: LinuxSshEngine
    platform: Platform
    client: CurlTool
    bmc_ip_address: str
    lockout_duration: int


@pytest.fixture
def bmc_password_test_context(engines, topology_obj) -> Iterator[BmcPasswordTestContext]:
    dut: LinuxSshEngine = engines.dut
    platform = Platform()
    client = CurlTool(
        server_host=PlatformConsts.BMC_INTERNAL_IP,
        username=BmcUsers.root.username,
        password=BmcUsers.root.another_password,
    )
    with allure.step('Get BMC addresses for test and cleanup'):
        bmc_ip_address = BmcTool.get_bmc_ip_addresses(engines, topology_obj)['IPv4']

    lockout_duration = BMC_LOCKOUT_DURATION_FALLBACK
    try:
        lockout_duration = get_bmc_account_lockout_duration(client)
        yield BmcPasswordTestContext(
            dut=dut,
            platform=platform,
            client=client,
            bmc_ip_address=bmc_ip_address,
            lockout_duration=lockout_duration,
        )
    finally:
        restore_bmc_root_password(platform, bmc_ip_address, lockout_duration)


@retry(Exception, tries=36, delay=10)
def reset_bmc_root_password_to_default(platform: Platform) -> None:
    platform.bmc_password.action_reset().verify_result()


def restore_bmc_root_password(platform: Platform, bmc_ip_address: str, lockout_duration: int) -> None:
    with allure.step('Restore BMC root password to lab password'):
        with allure.step('Wait for BMC network reachability'):
            ping_till_alive(True, bmc_ip_address, tries=180, delay=2)

        reset_bmc_root_password_to_default(platform)
        default_password = PlatformConsts.BMC_DEFAULT_ROOT_PASSWORD_AFTER_RESET_VIA_NOS
        client = CurlTool(
            server_host=PlatformConsts.BMC_INTERNAL_IP,
            username=BmcUsers.root.username,
            password=default_password,
        )
        output = client.change_root_password(
            password=default_password,
            new_password=BmcUsers.root.another_password,
            validate=True,
        )

        if REDFISH_AUTH_ERROR in output:
            with allure.step(f'Wait for BMC account lockout to expire ({lockout_duration}s)'):
                time.sleep(lockout_duration)
            reset_bmc_root_password_to_default(platform)
            output = client.change_root_password(
                password=default_password,
                new_password=BmcUsers.root.another_password,
                validate=True,
            )

        assert '"error"' not in output, f'Failed to restore BMC root password:\n{output}'


def get_bmc_account_lockout_duration(client: CurlTool) -> int:
    with allure.step('Read BMC AccountService AccountLockoutDuration'):
        output = client.run_redfish_command(rest_op='GET', path='/AccountService')
        account_service = OutputParsingTool.parse_json_str_to_dictionary(output).get_returned_value()
        lockout_duration = account_service.get('AccountLockoutDuration')
        assert isinstance(lockout_duration, int), (
            'BMC AccountService did not return a valid integer AccountLockoutDuration '
            f'(got {lockout_duration!r}). Raw Redfish output:\n{output}'
        )
        logger.info('BMC AccountLockoutDuration=%ss', lockout_duration)
        return lockout_duration
