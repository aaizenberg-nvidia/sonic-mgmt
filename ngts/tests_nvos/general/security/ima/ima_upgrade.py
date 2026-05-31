"""
Upgrade checker for the IMA ``state`` setting.

Plugs into the centralized upgrade test
(``ngts/tests_nvos/general/security/centralized_tests/upgrade/test_upgrade.py``)
and replaces the standalone ``test_ima_upgrade`` test. It follows the one-yield
checker convention used by the other security upgrade checkers: everything before
the ``yield`` is the pre-upgrade verification, everything after it is the
post-upgrade verification.

Behaviour mirrors the HLD / test-plan section 9.2:
  * after upgrade to the target image IMA is present and defaults to ``disabled``;
  * enabling IMA + reboot activates measurement on the upgraded image.
"""
from ngts.nvos_tools.infra.ImaTool import ImaTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.ima.constants import ImaConsts
from ngts.tests_nvos.general.security.ima.helpers import (
    build_reboot_params, save_config, set_ima_state, verify_ima_state)
from ngts.tests_nvos.general.upgrade_downgrade.feature_checkers import Skipped
from ngts.tools.test_utils import allure_utils as allure


def ima_upgrade_check():
    """
    Verify IMA works after upgrading to the target image.

    Pre (before ``yield``, on the base image): readiness gate and confirm IMA is at
    its default (``disabled``). Post (after ``yield``, on the target image): confirm
    IMA still defaults to ``disabled``, enable it, reboot, and verify measuring is
    active. IMA is disabled again before the centralized test cleanup runs.
    """
    engines = TestToolkit.engines
    devices = TestToolkit.devices
    dut_device = devices.dut

    if not getattr(dut_device, 'supports_ima_testing', False):
        raise Skipped('DUT device does not support IMA testing on this platform')
    if not ImaTool(engines.dut).is_ima_kernel_supported():
        raise Skipped('Running image kernel is not built with IMA (security filesystem path missing)')

    with allure.step('pre upgrade: IMA defaults to disabled on base image'):
        verify_ima_state(ImaConsts.DISABLED)

    yield  # upgrade to target image

    ima_tool = ImaTool(engines.dut)
    system = System()

    with allure.step('post upgrade: IMA is present and defaults to disabled'):
        verify_ima_state(ImaConsts.DISABLED)

    with allure.step('post upgrade: enable IMA, reboot, confirm measuring'):
        set_ima_state(ImaConsts.ENABLED)
        save_config(engines)
        system.action_reboot(
            reboot_params=build_reboot_params(devices, TestToolkit.topology_obj)).verify_result()
        verify_ima_state(ImaConsts.ENABLED)
        assert not ima_tool.has_no_file_measurements(), 'IMA is not measuring on the upgraded image'

    with allure.step('post upgrade: disable IMA before cleanup'):
        set_ima_state(ImaConsts.DISABLED)
        save_config(engines)

    yield  # prevent StopIteration on the 2nd next() call
