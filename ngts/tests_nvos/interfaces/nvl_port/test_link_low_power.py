import random
import re
import time
import pytest
import logging
from typing import Callable, Generator, Optional, TypedDict
from ngts.ngts_types.devices_T import DevicesT
from ngts.nvos_constants.constants_nvos import ConfState, NvosConst, LowPowerConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.interfaces.nvl_port import helpers as nvl_helpers
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.ib.InterfaceConfiguration.Link import LowPower

logger: logging.Logger = logging.getLogger(__name__)

OperationalAppliedOutputT = TypedDict('OperationalAppliedOutputT', {
    ConfState.OPERATIONAL: str,
    ConfState.APPLIED: str
})

LowPowerStateOutputT = TypedDict('LowPowerStateOutputT', {
    NvosConst.STATE: OperationalAppliedOutputT,
})

LowPowerFaeParamsOutputT = TypedDict('LowPowerFaeParamsOutputT', {
    LowPowerConsts.PEC_DURATION: OperationalAppliedOutputT,
    LowPowerConsts.PEC_RECAL_PERIOD: OperationalAppliedOutputT,
    LowPowerConsts.PEC_RECAL_FORCE_PERIOD: OperationalAppliedOutputT
})


LOW_POWER_STATE_REGEX = rf"{NvosConst.STATE}\s+({'|'.join([NvosConst.ENABLED, NvosConst.DISABLED])})\s+({'|'.join([NvosConst.ENABLED, NvosConst.DISABLED])})"
LOW_POWER_FAE_PARAMS_REGEX: dict[str, any] = {
    LowPowerConsts.PEC_DURATION: rf"{LowPowerConsts.PEC_DURATION}\s+(\d+)\s+(\d+)",
    LowPowerConsts.PEC_RECAL_PERIOD: rf"{LowPowerConsts.PEC_RECAL_PERIOD}\s+(\d+)\s+(\d+)",
    LowPowerConsts.PEC_RECAL_FORCE_PERIOD: rf"{LowPowerConsts.PEC_RECAL_FORCE_PERIOD}\s+({'|'.join(LowPowerConsts.PecRecalPeriodForce.all())})\s+({'|'.join(LowPowerConsts.PecRecalPeriodForce.all())})"
}


def should_skip_if_low_power_not_supported() -> bool:
    return is_bug_active(4831699) or is_bug_active(4850191)


def get_linked_ports_objs(devices: DevicesT, linked_ports_pair: tuple[str, str]) -> tuple[Port, Port]:
    if nvl_helpers.is_qtm3_device(devices):
        return tuple(Fae(port_name=port).port for port in linked_ports_pair)
    elif nvl_helpers.is_qtm4_device(devices):
        return tuple(Port(name=port) for port in linked_ports_pair)
    else:
        raise pytest.fail("Unsupported device type for low_power_obj fixture.")


@pytest.fixture(scope="session")
def linked_ports_objs(devices: DevicesT, linked_ports_pair: tuple[str, str]) -> tuple[Port, Port]:
    return get_linked_ports_objs(devices, linked_ports_pair)


def _get_low_power_output(port_obj: Port) -> LowPowerStateOutputT:
    with allure.step(f"Get {port_obj.name} low power output"):
        low_power_cmd: str = f"nv show {port_obj.interface.link.low_power.get_resource_path().replace('/', ' ')}"
        logger.info(f"Running command: {low_power_cmd}")
        low_power_output: str = TestToolkit.engines.dut.run_cmd(low_power_cmd)
        portlow_power_state_match = re.search(LOW_POWER_STATE_REGEX, low_power_output)
        assert portlow_power_state_match is not None, f"No low power state match found in output: {low_power_output}"
        return {
            NvosConst.STATE: {
                ConfState.OPERATIONAL: portlow_power_state_match.group(1),
                ConfState.APPLIED: portlow_power_state_match.group(2)
            }
        }


def low_power_state_case(
    devices: DevicesT,
    ports_to_set: Optional[list[tuple[Port, str]]] = None,
    expected_values: Optional[list[tuple[Port, str, str]]] = None
) -> None:
    if ports_to_set is not None:
        for port, value in ports_to_set:
            with allure.step(f"Set {NvosConst.STATE} to {value} on {port.name}"):
                port.interface.link.low_power.set(op_param_name=NvosConst.STATE, op_param_value=value, apply=True).verify_result()
        with allure.step("Wait for port state to be up"):
            seen = set()
            for port, _ in ports_to_set:
                if port.name in seen:
                    continue
                seen.add(port.name)
                port.interface.wait_for_port_state(
                    NvosConsts.LINK_STATE_UP,
                    timeout=nvl_helpers.acp_link_up_timeout(devices)).verify_result()
    if expected_values is not None:
        with allure.step("Verify link diagnostic"):
            ports: list[Port] = [port for port, _ in ports_to_set]
            nvl_helpers.verify_link_diagnostic(ports)
        with allure.step("Verify low power states"):
            for port, expected_operational, expected_applied in expected_values:
                output: LowPowerStateOutputT = _get_low_power_output(port)
                assert output[NvosConst.STATE][ConfState.OPERATIONAL] == expected_operational
                assert output[NvosConst.STATE][ConfState.APPLIED] == expected_applied


@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_link_low_power(devices: DevicesT, linked_ports_objs: tuple[Port, Port], test_api: ApiType):
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
    if should_skip_if_low_power_not_supported():
        pytest.skip("Low power is not supported")
    if not nvl_helpers.is_nvl_device(devices):
        pytest.skip("Low power is only supported on NVL devices")

    TestToolkit.tested_api = test_api
    with allure.step("Verify default low power states on both ports"):
        low_power_state_case(
            devices,
            expected_values=[((port_obj, NvosConst.DISABLED, NvosConst.DISABLED) for port_obj in linked_ports_objs)]
        )
    with allure.step("Verify low power states on both ports after setting to enabled"):
        low_power_state_case(
            devices,
            ports_to_set=[((port_obj, NvosConst.ENABLED) for port_obj in linked_ports_objs)],
            expected_values=[((port_obj, NvosConst.ENABLED, NvosConst.ENABLED) for port_obj in linked_ports_objs)]
        )
    with allure.step("Verify low power states when one port is set to enabled and the other is set to disabled"):
        low_power_state_case(
            devices,
            ports_to_set=[
                (linked_ports_objs[0], NvosConst.DISABLED)
            ],
            expected_values=[
                (linked_ports_objs[0], NvosConst.DISABLED, NvosConst.DISABLED),
                (linked_ports_objs[1], NvosConst.DISABLED, NvosConst.ENABLED)
            ]
        )
    with allure.step("Verify low power states after unset"):
        for port_obj in linked_ports_objs:
            port_obj.interface.link.low_power.unset(apply=True).verify_result()
        low_power_state_case(
            devices,
            expected_values=[((port_obj, NvosConst.DISABLED, NvosConst.DISABLED) for port_obj in linked_ports_objs)]
        )


def _get_low_power_fae_params_output(port_obj: Port) -> LowPowerFaeParamsOutputT:
    with allure.step(f"Get {port_obj.name} low power FAE params output"):
        tmp: str = port_obj.interface.link.low_power.get_resource_path().replace('/', ' ')
        tmp = tmp if "fae" in tmp else f"fae {tmp}"
        low_power_fae_params_cmd: str = f"nv show {tmp}"
        logger.info(f"Running command: {low_power_fae_params_cmd}")
        low_power_fae_params_output: str = TestToolkit.engines.dut.run_cmd(low_power_fae_params_cmd)
        output: LowPowerFaeParamsOutputT = {}
        for param, regex in LOW_POWER_FAE_PARAMS_REGEX.items():
            match = re.search(regex, low_power_fae_params_output)
            assert match is not None, f"No {param} match found in output: {low_power_fae_params_output}"
            output[param] = {
                ConfState.OPERATIONAL: match.group(1),
                ConfState.APPLIED: match.group(2)
            }
        return output


def _verify_fae_low_power_values(fae_low_power_obj: LowPower, expected_dict: dict[str, str]):
    with allure.step("Verify fae low power values"):
        output_dict = TestToolkit.tools.OutputParsingTool.parse_json_str_to_dictionary(fae_low_power_obj.show()).get_returned_value()
        for param, value in expected_dict.items():
            with allure.independent_step(f"Verify {param} operational and applied values"):
                ValidationTool.validate_field_value_in_output(output_dict, param, value).verify_result()


def _aggreed_function_exp_dict(set_dict: dict[str, str]) -> dict[str, str]:
    exp_dict: dict[str, str] = {}
    if LowPower.PEC_RECAL_FORCE_PERIOD in set_dict:
        exp_dict[LowPower.PEC_RECAL_FORCE_PERIOD] = set_dict[LowPower.PEC_RECAL_FORCE_PERIOD]
    if LowPower.PEC_DURATION in set_dict:
        exp_dict[LowPower.PEC_DURATION] = min(set_dict[LowPower.PEC_DURATION], LowPower.FAE_FIELDS[LowPower.PEC_DURATION]['default'])
    if LowPower.PEC_RECAL_PERIOD in set_dict:
        exp_dict[LowPower.PEC_RECAL_PERIOD] = max(set_dict[LowPower.PEC_RECAL_PERIOD], LowPower.FAE_FIELDS[LowPower.PEC_RECAL_PERIOD]['default'])
    return exp_dict


@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_link_low_power_fae_params(devices: DevicesT, linked_ports_pair: tuple[str, str], linked_ports_objs: tuple[Port, Port], test_api: ApiType):
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
    if should_skip_if_low_power_not_supported():
        pytest.skip("Low power is not supported")
    if not nvl_helpers.is_qtm4_device(devices):
        pytest.skip("Low power is only supported on NVL QTM4 devices")

    TestToolkit.tested_api = test_api
    fae_low_power_objs: tuple[LowPower, LowPower] = tuple(Fae(port_name=port).port.interface.link.low_power for port in linked_ports_pair)
    with allure.step("Verify low power states on both ports after setting to enabled"):
        low_power_state_case(
            devices,
            ports_to_set=[((port_obj, NvosConst.ENABLED) for port_obj in linked_ports_objs)],
            expected_values=[((port_obj, NvosConst.ENABLED, NvosConst.ENABLED) for port_obj in linked_ports_objs)]
        )
    with allure.step("Verify fae low power default values"):
        for fae_low_power_obj in fae_low_power_objs:
            _verify_fae_low_power_values(fae_low_power_obj, expected_dict={name: field["default"] for name, field in LowPower.FAE_FIELDS.items()})
    low_power_obj_1 = fae_low_power_objs[0]
    with allure.step("Verify fae low power set values"):
        fae_params_dict: dict[str, dict[str, dict[str, str]]] = {
            "min": {'set_dict': {name: field["values"][0] for name, field in LowPower.FAE_FIELDS.items().exclude(LowPower.PEC_RECAL_FORCE_PERIOD)}},
            "max": {'set_dict': {name: field["values"][-1] for name, field in LowPower.FAE_FIELDS.items().exclude(LowPower.PEC_RECAL_FORCE_PERIOD)}},
        }
        for fae_test in fae_params_dict:
            fae_params_dict[fae_test]['exp_dict'] = _aggreed_function_exp_dict(fae_params_dict[fae_test]['set_dict'])
        for fae_test in fae_params_dict:
            with allure.step(f"Verify fae low power set {fae_test} values"):
                for param, value in fae_params_dict[fae_test]['set_dict'].items():
                    low_power_obj_1.set(op_param_name=param, op_param_value=value, apply=True).verify_result()
                _verify_fae_low_power_values(low_power_obj_1, expected_dict=fae_params_dict[fae_test]['exp_dict'])
    with allure.step("Verify fae low power random values"):
        fae_params_dict: dict[str, dict[str, dict[str, str]]] = {
            "random": {'set_dict': {name: random.choice(field["values"]) for name, field in LowPower.FAE_FIELDS.items().exclude(LowPower.PEC_RECAL_FORCE_PERIOD)}}
        }
        fae_params_dict['random']['exp_agreed_dict'] = _aggreed_function_exp_dict(fae_params_dict['random']['set_dict'])
        for param, value in fae_params_dict['random']['set_dict'].items():
            low_power_obj_1.set(op_param_name=param, op_param_value=value, apply=True).verify_result()
        with allure.step(f"Verify fae low power when {LowPower.PEC_RECAL_FORCE_PERIOD} is {LowPower.PecRecalPeriodForce.USE_AGREED_FUNCTION}"):
            _verify_fae_low_power_values(low_power_obj_1, expected_dict=fae_params_dict['random']['exp_agreed_dict'])
        with allure.step(f"Verify fae low power when {LowPower.PEC_RECAL_FORCE_PERIOD} is {LowPower.PecRecalPeriodForce.FORCE_PERIOD}"):
            low_power_obj_1.set(op_param_name=LowPower.PEC_RECAL_FORCE_PERIOD, op_param_value=LowPower.PecRecalPeriodForce.FORCE_PERIOD, apply=True).verify_result()
            _verify_fae_low_power_values(low_power_obj_1, expected_dict=fae_params_dict['random']['set_dict'])
        with allure.step(f"Verify fae low power when both ports are set to {LowPower.PecRecalPeriodForce.FORCE_PERIOD}"):
            low_power_obj_2 = fae_low_power_objs[1]
            low_power_obj_2.set(op_param_name=LowPower.PEC_RECAL_FORCE_PERIOD, op_param_value=LowPower.PecRecalPeriodForce.FORCE_PERIOD, apply=True).verify_result()
            _verify_fae_low_power_values(low_power_obj_1, expected_dict=fae_params_dict['random']['exp_agreed_dict'])
    with allure.step("Verify fae low power unset values"):
        for fae_low_power_obj in fae_low_power_objs:
            fae_low_power_obj.unset(op_param_name=NvosConst.ENABLED, apply=True).verify_result()
            _verify_fae_low_power_values(fae_low_power_obj, expected_dict={name: field["default"] for name, field in LowPower.FAE_FIELDS.items()})
    with allure.step("Verify fae low power counters"):
        output_dict = TestToolkit.tools.OutputParsingTool.parse_json_str_to_dictionary(fae_low_power_objs[0].show(op_param=LowPower.COUNTERS)).get_returned_value()
        initial_counters: dict[str, str] = {}
        for counter in LowPower.ALL_COUNTERS:
            assert output_dict.get(counter) is not None, f"Counter {counter} is not found in the output"
            initial_counters[counter] = output_dict.get(counter)
        time.sleep(10)
        output_dict = TestToolkit.tools.OutputParsingTool.parse_json_str_to_dictionary(fae_low_power_objs[0].show(op_param=LowPower.COUNTERS)).get_returned_value()
        for counter in LowPower.ALL_COUNTERS:
            assert output_dict.get(counter) is not None, f"Counter {counter} is not found in the output"
            assert output_dict.get(counter) > initial_counters[counter], f"Counter {counter} is not greater than {initial_counters[counter]}, got: {output_dict.get(counter)}"
