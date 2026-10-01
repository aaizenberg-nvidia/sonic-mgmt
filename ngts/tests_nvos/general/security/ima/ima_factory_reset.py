"""
Factory-reset checkers for the IMA ``state`` setting.

These plug into the centralized factory-reset test
(``ngts/tests_nvos/general/security/centralized_tests/factory_reset/test_factory_reset.py``)
and replace the standalone ``test_ima_factory_reset`` test. They follow the
one-yield checker convention used by the other security checkers: everything before
the ``yield`` is the pre-factory-reset setup, everything after it is the
post-factory-reset verification.

Behaviour mirrors the HLD / test-plan section 9.1:
  * config-wiping resets (no-params / keep-basic / keep-only-files) -> IMA back to
    its default (``disabled``);
  * keep-all-config -> the IMA setting is preserved (``enabled``).

Only the NVUE config value (``nv show system security ima``) is checked, which is
visible right after ``set`` + ``apply`` + ``save`` - no reboot is required to assert
config persistence across a factory reset.
"""
from ngts.nvos_tools.infra.ImaTool import ImaTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.general.security.ima.constants import ImaConsts
from ngts.tests_nvos.general.security.ima.helpers import save_config, set_ima_state, verify_ima_state
from ngts.tests_nvos.general.upgrade_downgrade.feature_checkers import Skipped
from ngts.tools.test_utils import allure_utils as allure


def _ima_factory_reset_check(expected_state_after_reset: str):
    """
    Generic IMA factory-reset checker.

    Pre (before ``yield``): enable IMA and save the config. Post (after ``yield``):
    assert the IMA ``state`` matches ``expected_state_after_reset``.

    Skips (via :class:`Skipped`) on setups that cannot exercise IMA - mirroring the
    IMA suite's readiness gating (TPM present + IMA-enabled kernel).

    :param expected_state_after_reset: IMA ``state`` expected once the reset is done -
        ``disabled`` when the config is wiped, ``enabled`` when it is preserved.
    """
    engines = TestToolkit.engines
    dut_device = TestToolkit.devices.dut

    if not getattr(dut_device, 'supports_ima_testing', False):
        raise Skipped('DUT device does not support IMA testing on this platform')
    if not ImaTool(engines.dut).is_ima_kernel_supported():
        raise Skipped('Running image kernel is not built with IMA (security filesystem path missing)')

    with allure.step('pre factory reset: enable IMA and save the config'):
        set_ima_state(ImaConsts.ENABLED)
        save_config(engines)

    yield  # factory reset

    with allure.step(f'post factory reset: IMA state is "{expected_state_after_reset}"'):
        verify_ima_state(expected_state_after_reset)

    yield  # to prevent StopIteration on the 2nd next() call


def ima_factory_reset_reset_to_default_check():
    """Config-wiping reset (no-params / keep-basic / keep-only-files): IMA -> default (disabled)."""
    yield from _ima_factory_reset_check(ImaConsts.DISABLED)


def ima_factory_reset_keep_all_config_check():
    """keep-all-config: the IMA setting is preserved (enabled)."""
    yield from _ima_factory_reset_check(ImaConsts.ENABLED)
