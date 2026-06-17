import logging
import random

import pytest
from retry.api import retry_call

from infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_constants.constants_nvos import HealthConsts, PlatformConsts
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.infra.CurlTool import CurlTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.NvCommand import NvCommand
from ngts.nvos_tools.platform.BmcFactoryDefault import BmcFactoryDefaultErrors, BmcFactoryDefaultMode
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import BmcUsers, CURL_AUTHORIZATION_ERR_MSGS
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


def require_bmc_platform(devices):
    if not getattr(devices.dut, 'has_bmc', False):
        pytest.skip('BMC-equipped platform required')


@pytest.mark.disable_loganalyzer
@pytest.mark.bmc
def test_bmc_factory_default_flow(engines, devices, topology_obj, nv_command: NvCommand, random_api):
    """
    1. randomly select mode: default | config-only | config-and-logs | secure-erase
    2. factory reset the BMC via NVUE or OpenAPI
    3. wait for BMC to be reachable again
    4. verify root password and restore lab default if needed
    5. verify health status is OK
    """
    require_bmc_platform(devices)
    TestToolkit.tested_api = random_api
    dut: LinuxSshEngine = engines.dut
    platform = Platform()

    with allure.step('Select mode (including default)'):
        mode, mode_label = random.choice([
            (None, 'default'),
            (BmcFactoryDefaultMode.CONFIG_ONLY, 'config-only'),
            (BmcFactoryDefaultMode.CONFIG_AND_LOGS, 'config-and-logs'),
            (BmcFactoryDefaultMode.SECURE_ERASE, 'secure-erase'),
        ])
        logger.info('Selected BMC factory-default mode=%s', mode_label)

    with allure.step(f'Run nv action reset platform bmc-factory-default mode {mode_label} force'):
        platform.bmc_factory_default.action_reset(
            mode=mode,
            force=True,
        ).verify_result()

    if mode == BmcFactoryDefaultMode.SECURE_ERASE:
        root_password = BmcUsers.root.another_password
    else:
        root_password = BmcUsers.root.default_password

    with allure.step('Wait for BMC to be reachable again'):
        client = CurlTool(server_host=PlatformConsts.BMC_INTERNAL_IP, username=BmcUsers.root.username,
                          password=root_password)
        assert client.wait_for_bmc_available(dut_engine=dut), 'BMC did not become reachable'
        BmcTool.wait_for_cpu_to_detect_bmc(dut, nv_command)

    if mode != BmcFactoryDefaultMode.SECURE_ERASE:
        with allure.step('Connect to BMC after reset and replace password with the lab password.'):
            out = client.change_root_password(password=BmcUsers.root.default_password)
            auth_success = all(err_msg not in out for err_msg in CURL_AUTHORIZATION_ERR_MSGS)
            assert auth_success, f'expected root factory default password to work.\nout:\n{out}'

    with allure.step('Connect to BMC and verify health is OK'):
        out = client.run_redfish_command(rest_op='GET', path='/Managers/BMC_0', dut_engine=dut,
                                         password=BmcUsers.root.another_password)
        assert '"Health": "OK"' in out, f'expected BMC health OK.\nout:\n{out}'

    with allure.step('Verify health status is OK'):
        retry_call(nv_command.system.validate_health_status, fargs=[HealthConsts.OK], tries=4, delay=30,
                   exceptions=AssertionError, logger=logger)


@pytest.mark.disable_loganalyzer
@pytest.mark.bmc
def test_reset_platform_bmc_factory_default_rejection(engines, devices, topology_obj, nv_command: NvCommand,
                                                      random_api):
    """
    nv action reset platform bmc-factory-default is blocked when BMC is unreachable.

    1. graceful-restart the BMC via Redfish (BMC unreachable)
    2. randomly select mode
    3. run factory reset with force — expected to fail with a clear message
    4. wait for BMC to be reachable again
    """
    require_bmc_platform(devices)
    TestToolkit.tested_api = random_api
    dut: LinuxSshEngine = engines.dut
    platform = Platform()
    client = CurlTool(server_host=PlatformConsts.BMC_INTERNAL_IP, username=BmcUsers.root.username,
                      password=BmcUsers.root.another_password)

    try:
        with allure.step('Graceful-restart BMC via Redfish (BMC becomes unreachable)'):
            BmcTool.reset(dut)
            assert client.wait_for_bmc_unavailable(dut_engine=dut), 'BMC should be unreachable after restart'

        with allure.step('Select mode (including default)'):
            mode, mode_label = random.choice([
                (None, 'default'),
                (BmcFactoryDefaultMode.CONFIG_ONLY, 'config-only'),
                (BmcFactoryDefaultMode.CONFIG_AND_LOGS, 'config-and-logs'),
                (BmcFactoryDefaultMode.SECURE_ERASE, 'secure-erase'),
            ])
            logger.info('Selected BMC factory-default mode=%s', mode_label)

        with allure.step(f'Run nv action reset platform bmc-factory-default mode {mode_label} force — expect failure'):
            platform.bmc_factory_default.action_reset(
                mode=mode,
                force=True,
            ).verify_result(should_succeed=False, expected_value=BmcFactoryDefaultErrors.CONNECT_ERROR)
    finally:
        with allure.step('Wait for BMC to be reachable again'):
            assert client.wait_for_bmc_available(dut_engine=dut), 'BMC did not become reachable'
            BmcTool.wait_for_cpu_to_detect_bmc(dut, nv_command)
