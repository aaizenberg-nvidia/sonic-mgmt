import logging
import re

import pytest
import time
from ngts.nvos_constants.constants_nvos import NvosConst, PlatformConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RegressionConfigurations import RegressionConfigurations
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.ib.opensm.OpenSmTool import OpenSmTool
from ngts.tools.test_utils.nvos_general_utils import check_partitions_capacity


cumulus_owner = "hiept"


logger = logging.getLogger()


@pytest.mark.init_flow
@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.nvos_ci
def test_system_services(engines, devices):
    """
    Verifying the NVOS system services are in active state
    Run sudo systemctl status and validate systemctl state is active and no jobs or failures"
    Run sudo systemctl is-active hw-management and validate hw_management is active"
    :return: None, raise error in case one or more services are inactive
    """
    with allure.step("Validate services are active"):
        res_obj = devices.dut.verify_services(engines.dut)
        assert res_obj.result, res_obj.info


@pytest.mark.cumulus
@pytest.mark.init_flow
def test_partitions_capacity(devices):
    check_partitions_capacity(partition_name=devices.dut.disk_default_partition_name,
                              allowed_limit=devices.dut.disk_partition_capacity_limit,
                              minimum_free_space=devices.dut.disk_minimum_free_space)


@pytest.mark.init_flow
@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.nvos_ci
def test_system_dockers(engines, devices):
    """
    Verifying the NVOS system dockers are up
    Run "docker ps" and validate the expected dockers appears
    :return: None, raise error in case one or more dockers are down
    """
    with allure.step("Validate docker are up"):
        res_obj = devices.dut.verify_dockers(engines.dut)
        assert res_obj.result, res_obj.info


@pytest.mark.init_flow
@pytest.mark.nvos_ci
def test_existence_of_tables_in_databases(engines, devices):
    """
    Verifying the NVOS Databases created the correct tables in redis
    :return: None, raise error in case one or more tables are missed
    """
    with allure.step("Validate no missing database default tables"):
        res_obj = devices.dut.verify_databases(engines.dut)
        assert res_obj.result, res_obj.info


@pytest.mark.init_flow
def test_ports_are_up(engines, devices):
    """
    Verifying the NVOS ports are up
    :return: None, raise error in case one or more ports are down
    """
    if NvosConst.HOST_HA not in engines:
        pytest.skip("Test skipped since no traffic host connected")

    result = OpenSmTool.start_open_sm(engines)
    assert result.result, "Failed to start openSM"

    RegressionConfigurations.configure_ports_to_legacy(engine=engines.dut, apply=True, throw_exception=True,
                                                       wait_till_port_up=True)
    with allure.step("Validate all ports status is up"):
        res_obj = devices.dut.verify_ib_ports_state(engines.dut, NvosConst.PORT_STATUS_UP)
        assert res_obj.result, res_obj.info


@pytest.mark.init_flow
@pytest.mark.nvos_ci
def test_check_firmware(engines):
    """
    Verify installed firmware is equal to actual firmware
    """
    with allure.step("Verify installed firmware is equal to actual firmware"):
        fae = Fae()
        all_firmware = OutputParsingTool.parse_json_str_to_dictionary(fae.platform.firmware.show()).get_returned_value()
        errors = []
        for item, properties in all_firmware.items():
            if not re.fullmatch(PlatformConsts.FW_ASIC + r'\d*', item):
                continue  # check only the asics, not bios and other firmware
            logger.info(f"Checking {item}")
            installed_fw = properties['installed-firmware']
            actual_fw = properties['actual-firmware']
            if installed_fw != actual_fw:
                errors.append(f"{item} : {installed_fw=}, {actual_fw=}")
        assert not errors, f"{len(errors)} ASICs have installed-fw != actual-fw:\n" + '\n'.join(errors)
