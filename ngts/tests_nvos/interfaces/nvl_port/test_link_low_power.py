import random
import time
import pytest

from ngts.ngts_types import CleanUpT, DevicesT
from ngts.nvos_constants import constants_nvos as consts_nv
from ngts.nvos_tools.infra import Fae
from ngts.nvos_tools.infra import NvosTestToolkit as TestToolkit
from ngts.tests_nvos.helpers.interfaces.nvl_port import low_power_helpers
from ngts.tests_nvos.interfaces.nvl_port import helpers as nvl_helpers
from ngts.tools.test_utils import allure_utils as allure


@pytest.mark.parametrize('test_api', [random.choice(consts_nv.ApiType.ALL_TYPES)])
def test_link_low_power(devices: DevicesT, linked_ports_pair: tuple[str, str], test_api: consts_nv.ApiType):
    """
    Test low power functionality on NVL ports.
    This test verifies the low power feature on NVL devices, which allows a port to be configured to
    a specific low power state (enabled or disabled). The test validates that the port's low power state
    is set correctly and remains stable across port state flips.
    Test Steps:
        1. Verify default low power states on both ports
        2. Verify low power states on both ports after setting to enabled
        3. Verify low power states when one port is set to enabled and the other is set to disabled
        4. Verify low power states after unset
    """
    if low_power_helpers.should_skip_if_low_power_not_supported():
        pytest.skip("Low power is not supported")
    if not nvl_helpers.is_nvl_device(devices):
        pytest.skip("Low power is only supported on NVL devices")

    TestToolkit.TestToolkit.tested_api = test_api
    linked_ports_objs = low_power_helpers.get_linked_ports_objs(devices, linked_ports_pair)

    with allure.step("Verify default low power states on both ports"):
        low_power_helpers.low_power_state_case(
            devices,
            expected_values=[
                (port_obj, consts_nv.NvosConst.DISABLED, consts_nv.NvosConst.DISABLED)
                for port_obj in linked_ports_objs
            ],
        )
    with allure.step("Verify low power states on both ports after setting to enabled"):
        low_power_helpers.low_power_state_case(
            devices,
            ports_to_set=[(port_obj, consts_nv.NvosConst.ENABLED) for port_obj in linked_ports_objs],
            expected_values=[
                (port_obj, consts_nv.NvosConst.ENABLED, consts_nv.NvosConst.ENABLED)
                for port_obj in linked_ports_objs
            ],
        )
    with allure.step("Verify low power states when one port is set to enabled and the other is set to disabled"):
        low_power_helpers.low_power_state_case(
            devices,
            ports_to_set=[(linked_ports_objs[0], consts_nv.NvosConst.DISABLED)],
            expected_values=[
                (linked_ports_objs[0], consts_nv.NvosConst.DISABLED, consts_nv.NvosConst.DISABLED),
                (linked_ports_objs[1], consts_nv.NvosConst.DISABLED, consts_nv.NvosConst.ENABLED),
            ],
        )
    with allure.step("Verify low power states after unset"):
        low_power_helpers.cleanup_low_power(devices, linked_ports_objs)


@pytest.mark.parametrize('test_api', [random.choice(consts_nv.ApiType.ALL_TYPES)])
def test_link_low_power_fae_params(
    devices: DevicesT,
    linked_ports_pair: tuple[str, str],
    test_api: consts_nv.ApiType,
    register_cleanup: CleanUpT,
):
    """
    Test low power functionality on NVL ports with FAE parameters.
    This test verifies the low power feature on NVL devices with FAE parameters, which allows a port to be configured to
    a specific low power state (enabled or disabled). The test validates that the port's low power state
    is set correctly and remains stable across port state flips.
    Test Steps:
        1. Verify low power states on both ports after setting to enabled
        2. Verify fae low power default values
        3. Verify fae low power set values
        4. Verify fae low power random values
        5. Verify fae low power unset values
        6. Verify fae low power counters
    """
    if low_power_helpers.should_skip_if_low_power_not_supported():
        pytest.skip("Low power is not supported")
    if not nvl_helpers.is_qtm4_device(devices):
        pytest.skip("Low power is only supported on NVL QTM4 devices")

    TestToolkit.TestToolkit.tested_api = test_api
    consts = consts_nv.LowPowerConsts
    linked_ports_objs = low_power_helpers.get_linked_ports_objs(devices, linked_ports_pair)
    fae_low_power_objs = tuple(
        Fae.Fae(port_name=port).port.interface.link.low_power for port in linked_ports_pair
    )
    # the peq params are unset by the test itself; this restores the low-power state it enables
    register_cleanup(lambda: low_power_helpers.cleanup_low_power(devices, linked_ports_objs))

    with allure.step("Verify low power states on both ports after setting to enabled"):
        low_power_helpers.low_power_state_case(
            devices,
            ports_to_set=[(port_obj, consts_nv.NvosConst.ENABLED) for port_obj in linked_ports_objs],
            expected_values=[
                (port_obj, consts_nv.NvosConst.ENABLED, consts_nv.NvosConst.ENABLED)
                for port_obj in linked_ports_objs
            ],
        )
    with allure.step("Verify fae low power default values"):
        for fae_low_power_obj in fae_low_power_objs:
            low_power_helpers.verify_low_power(fae_low_power_obj, low_power_helpers.fae_low_power_defaults())

    low_power_obj_1, low_power_obj_2 = fae_low_power_objs
    # peq-recal-force-period is driven explicitly below, so it is excluded from the value sweeps
    swept_fields = {
        name: field for name, field in consts.FAE_FIELDS.items() if name != consts.PEC_RECAL_FORCE_PERIOD
    }
    with allure.step("Verify fae low power set values"):
        set_dicts: dict[str, dict[str, str]] = {
            "min": {name: field["values"][0] for name, field in swept_fields.items()},
            "max": {name: field["values"][-1] for name, field in swept_fields.items()},
        }
        for case_name, set_dict in set_dicts.items():
            with allure.step(f"Verify fae low power set {case_name} values"):
                _set_fae_params(low_power_obj_1, set_dict)
                # peq-recal-force-period is at its use-agreed-function default, so operational holds
                # the value negotiated against the peer's defaults while applied holds what was set
                low_power_helpers.verify_low_power(low_power_obj_1, {
                    consts_nv.ConfState.OPERATIONAL: low_power_helpers.agreed_function_expected(set_dict),
                    consts_nv.ConfState.APPLIED: set_dict,
                })

    with allure.step("Verify fae low power random values"):
        random_set_dict = {name: random.choice(field["values"]) for name, field in swept_fields.items()}
        random_agreed_dict = low_power_helpers.agreed_function_expected(random_set_dict)
        _set_fae_params(low_power_obj_1, random_set_dict)
        with allure.step(f"Verify fae low power when {consts.PEC_RECAL_FORCE_PERIOD} is {consts.PecRecalPeriodForce.USE_AGREED_FUNCTION.value}"):
            low_power_helpers.verify_low_power(low_power_obj_1, {
                consts_nv.ConfState.OPERATIONAL: random_agreed_dict,
                consts_nv.ConfState.APPLIED: random_set_dict,
            })
        with allure.step(f"Verify fae low power when {consts.PEC_RECAL_FORCE_PERIOD} is {consts.PecRecalPeriodForce.FORCE_PERIOD.value}"):
            _set_fae_params(
                low_power_obj_1,
                {consts.PEC_RECAL_FORCE_PERIOD: consts.PecRecalPeriodForce.FORCE_PERIOD.value},
            )
            # forcing the period on one end only means its own values win the negotiation
            low_power_helpers.verify_low_power(
                low_power_obj_1, low_power_helpers.expected_operational_applied(random_set_dict),
            )
        with allure.step(f"Verify fae low power when both ports are set to {consts.PecRecalPeriodForce.FORCE_PERIOD.value}"):
            _set_fae_params(
                low_power_obj_2,
                {consts.PEC_RECAL_FORCE_PERIOD: consts.PecRecalPeriodForce.FORCE_PERIOD.value},
            )
            low_power_helpers.verify_low_power(low_power_obj_1, {
                consts_nv.ConfState.OPERATIONAL: random_agreed_dict,
                consts_nv.ConfState.APPLIED: random_set_dict,
            })

    with allure.step("Verify fae low power unset values"):
        for fae_low_power_obj in fae_low_power_objs:
            # unset the peq params only - the low-power state must stay enabled for the counters step
            for index, param in enumerate(consts.FAE_ALL_PARAMS):
                is_last = index == len(consts.FAE_ALL_PARAMS) - 1
                fae_low_power_obj.unset(op_param=param, apply=is_last, ask_for_confirmation=True).verify_result()
            low_power_helpers.verify_low_power(fae_low_power_obj, low_power_helpers.fae_low_power_defaults())

    with allure.step("Verify fae low power counters"):
        initial_counters = _get_counters(low_power_obj_1)
        time.sleep(10)
        current_counters = _get_counters(low_power_obj_1)
        for counter, initial_value in initial_counters.items():
            assert current_counters[counter] > initial_value, (
                f"Counter {counter} value is {current_counters[counter]} "
                f"- expected it to be greater than {initial_value}"
            )


def _set_fae_params(low_power_component, set_dict: dict[str, str]) -> None:
    """Set the given fae low-power params, applying once at the end."""
    params = list(set_dict.items())
    for index, (param, value) in enumerate(params):
        low_power_component.set(
            op_param_name=param,
            op_param_value=value,
            apply=index == len(params) - 1,
            ask_for_confirmation=True,
        ).verify_result()


def _get_counters(low_power_component) -> dict[str, int]:
    output = low_power_component.parse_show(op_param=consts_nv.LowPowerConsts.COUNTERS)
    for counter in consts_nv.LowPowerConsts.ALL_COUNTERS:
        assert output.get(counter) is not None, f"Counter {counter} value is missing from the output {output}"
    return {counter: int(output[counter]) for counter in consts_nv.LowPowerConsts.ALL_COUNTERS}
