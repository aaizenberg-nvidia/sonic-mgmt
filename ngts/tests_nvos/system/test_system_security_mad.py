import logging

import pytest

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.mark.system
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_system_security_gpu_mad(test_api, engines, devices, nv_command):
    """
    Verify show/set/unset system security gpu-mad state command
        Test flow:
            1.  Run show system security gpu-mad command
            2.  Verify gpu-mad field is present
            3.  Run set system security gpu-mad command to enable it
            5.  Run show system security gpu-mad to verify that gpu-mad is enabled
            6.  Verify value gpu-mad state is updated as enabled in register
            7.  Run set system security gpu-mad command to disable it
            8.  Run show system security gpu-mad to verify that gpu-mad is disabled
            9.  Verify value gpu-mad state is updated as disabled in register
            10. Unset system security gpu-mad to set it to default(enabled)
            11. Run show system security gpu-mad to verify that gpu-mad is set to default(enabled)
            12. Verify value gpu-mad state is updated as default(enabled) in register
    """
    TestToolkit.tested_api = test_api

    try:
        with allure.step('Run show system security gpu-mad command and verify field is present'):
            output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.security.gpumad.show()).\
                get_returned_value()
            ValidationTool.verify_field_exist_in_json_output(output, [SystemConsts.STATE]).verify_result()

        with allure.step('Run set system security gpu-mad command to enable it'):
            state = SystemConsts.GPU_MAD_STATE_ENABLED
            nv_command.system.security.gpumad.set(op_param_name=SystemConsts.STATE, op_param_value=state, apply=True).\
                verify_result()

        with allure.step('Run show system security gpu-mad to verify that gpu-mad is enabled'):
            output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.security.gpumad.show())\
                .get_returned_value()
            ValidationTool.verify_field_value_in_output(output, SystemConsts.STATE, state).verify_result()

        with allure.step('Verify value gpu-mad state is updated as enabled in register'):
            assert state == helper_gpu_mad_reg_read(engines), "GPU-MAD is not enabled in register"

        with allure.step('Run set system security gpu-mad command to disable it'):
            state = SystemConsts.GPU_MAD_STATE_DISABLED
            nv_command.system.security.gpumad.set(op_param_name=SystemConsts.STATE, op_param_value=state, apply=True).\
                verify_result()

        with allure.step('Run show system security gpu-mad to verify that gpu-mad is disabled'):
            output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.security.gpumad.show())\
                .get_returned_value()
            ValidationTool.verify_field_value_in_output(output, SystemConsts.STATE, state).verify_result()

        with allure.step('Verify value gpu-mad state is updated as disabled in register'):
            assert state == helper_gpu_mad_reg_read(engines), "GPU-MAD is not disabled in register"

    finally:
        with allure.step('Run unset system security gpu-mad state'):
            nv_command.system.security.gpumad.unset(apply=True).verify_result()

        with allure.step('Run show system security gpu-mad to verify that gpu-mad is default(enabled)'):
            state = SystemConsts.GPU_MAD_STATE_ENABLED
            output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.security.gpumad.show())\
                .get_returned_value()
            ValidationTool.verify_field_value_in_output(output, SystemConsts.STATE, state).verify_result()

        with allure.step('Verify value gpu-mad state is updated as default(enabled) in register'):
            assert state == helper_gpu_mad_reg_read(engines), "GPU-MAD is not enabled in register"


@pytest.mark.system
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_system_security_gpu_mad_invalid(test_api, engines, devices, nv_command):
    """
    Verify show/set/unset system security gpu-mad state command
        Test flow:
            1.  Run show system security gpu-mad command, get the state value
            2.  Get the register value for GPU MAD state
            3.  Run set system security gpu-mad command to set to invalid value, verify it fails
            5.  Run show system security gpu-mad to verify that gpu-mad is unchanged
            6.  Get the register value for GPU MAD state to verify it is unchanged
    """
    TestToolkit.tested_api = test_api
    invalid_value_set = True
    try:
        with allure.step('Run show system security gpu-mad command and get the state'):
            output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.security.gpumad.show()).\
                get_returned_value()
            state = output[f'{SystemConsts.STATE}']

        with allure.step('Check register value for GPU MAD state'):
            reg_state = helper_gpu_mad_reg_read(engines)

        with allure.step('Run set system security gpu-mad command to set invalid value'):
            nv_command.system.security.gpumad.set(op_param_name=SystemConsts.STATE, op_param_value="invalid",
                                                  apply=True).verify_result(should_succeed=False)

        with allure.step('Run show system security gpu-mad to verify that gpu-mad is unchanged'):
            output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.security.gpumad.show())\
                .get_returned_value()
            new_state = output[f'{SystemConsts.STATE}']
            assert state == new_state, f"GPU MAD state changed from {state} to {new_state}"

        with allure.step('Verify register value for gpu-mad state is unchanged'):
            reg_state_new = helper_gpu_mad_reg_read(engines)
            assert reg_state == reg_state_new, f"Register val for GPU MAD state changed {reg_state} to {reg_state_new}"

        invalid_value_set = False

    finally:
        if invalid_value_set:
            with allure.step('Run unset system security gpu-mad state to clear invalid config'):
                nv_command.system.security.gpumad.unset(apply=True).verify_result()


def helper_gpu_mad_reg_read(engines):
    output = engines.dut.run_cmd("sudo mcra /dev/mst/mt54008_pciconf0 0x21820c.2:1")
    if output == "0x00000000":
        return SystemConsts.GPU_MAD_STATE_ENABLED
    elif output == "0x00000001":
        return SystemConsts.GPU_MAD_STATE_DISABLED
    return None
