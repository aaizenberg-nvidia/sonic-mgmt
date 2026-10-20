import logging
import time
import pytest

from ngts.nvos_constants.constants_nvos import ApiType, NvosConst
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RegisterTool import RegisterTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.tests_nvos.constants import MINUTE
logger = logging.getLogger()


@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.interfaces
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_link_low_power_disabled(engines, devices, prepare_traffic, test_api):
    """
    validate feature default values using ports and show command

    to validate ports we will use:
        mlxreg -d /dev/mst/mt54004_pciconf0 --reg_name PPSLS -g --indexes "lp_msb=8,local_port={port_number}

    Test flow:
    1. Run nv show ib link-low-power
    2. validate feature disabled
    3. validate for all ports l1_cap = 1
    """
    TestToolkit.tested_api = test_api

    _test_l1_behavior(engine=engines.dut, device=devices.dut, expected_state=NvosConst.DISABLED, expected_l1_cap_value=0)

    # TODO - once we can send traffic we need to add case sending traffic and validate
    #  no time difference between sending with feature enabled or disabled.


@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.interfaces
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_link_low_power_enabled(engines, devices, topology_obj, prepare_traffic, test_api):
    """
    testing enabling the feature and verify show command and ports

    Test flow:
    1. Run nv set fae ib link-low-power state enabled + apply
    2. Run nv show ib link-low-power
    3. validate feature enabled
    4. validate for all ports l1_cap = 1
    5. Run nv unset fae ib link-low-power state + apply
    6. Run nv show ib link-low-power
    7. validate feature disabled
    8. validate for all ports l1_cap = 0
    """
    TestToolkit.tested_api = test_api
    fae = Fae()

    try:
        with allure.step("disabled l1 saving power capability"):
            fae.ib.link_low_power.set(op_param_name=IbInterfaceConsts.LINK_STATE, op_param_value=NvosConst.ENABLED, apply=True)
            with allure.step("wait until ports update"):
                time.sleep(50)

            _test_l1_behavior(engine=engines.dut, device=devices.dut, expected_state=NvosConst.ENABLED, expected_l1_cap_value=1)

    finally:
        fae.ib.link_low_power.unset(IbInterfaceConsts.LINK_STATE, apply=True)
        with allure.step("wait until ports update"):
            time.sleep(50)
        _test_l1_behavior(engine=engines.dut, device=devices.dut, expected_state=NvosConst.DISABLED, expected_l1_cap_value=0)


def validate_all_ports_l1_capability(expected_status, engine, device):
    """

    :param expected_status: 0/1
    :param engine:
    :param device:
    :return:
    """
    err_msg = ""
    mst_path = "/dev/mst/"
    mst_devices = engine.run_cmd(f"ls {mst_path} | grep -i pciconf").splitlines()
    ""
    with allure.step(f"Verify all ports support L1 saving power is {expected_status}"):
        for port in range(1, device.valid_ports_count * 2, 2):
            logger.info(f"validate l1_cap for port {port}:")
            for device in mst_devices:
                output = RegisterTool.get_mst_register_value(engine, mst_path + device, "PPSLS", f'--indexes "lp_msb=8,local_port={port}"', "l1_cap")
            digit = output.strip()[-1]
            if digit != str(expected_status):
                err_msg += f"port {port} L1 capability is {digit} not as expected {expected_status}\n"

    assert not err_msg, err_msg


def _test_l1_behavior(engine, device, expected_state, expected_l1_cap_value):
    """
    :param engine:
    :param device
    :param expected_state: enabled/disabled
    :param expected_l1_cap_value: 0/1
    :return:
    """
    fae = Fae()
    with allure.step(f"verify link low power value is {expected_state}"):
        show_output = OutputParsingTool.parse_json_str_to_dictionary(fae.ib.link_low_power.show()).verify_result()
        ValidationTool.verify_field_value_in_output(show_output, IbInterfaceConsts.LINK_STATE, expected_state)

        validate_all_ports_l1_capability(expected_status=expected_l1_cap_value, engine=engine, device=device)
