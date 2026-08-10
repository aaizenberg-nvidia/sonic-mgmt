import logging
import pytest

from ngts.nvos_tools.Devices.IbDevice import JulietSwitch
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_constants.constants_nvos import PlatformConsts, NvosConst, ImageConsts
from ngts.nvos_constants.constants_nvos import ApiType, OutputFormat
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.platform.firmware_telemetry_helpers import (
    assert_gnmi_firmware_version_matches_nvue,
    expand_nvue_key_to_gnmi_components,
)
from ngts.tests_nvos.system.reboot_telemetry_helpers import gnmi_client_for_dut


cumulus_owner = "hiept"


logger = logging.getLogger()


@pytest.mark.platform
@pytest.mark.cumulus
@pytest.mark.nvos_ci
@pytest.mark.nvl_ci
@pytest.mark.air
@pytest.mark.air_ci
@pytest.mark.air_sanity
@pytest.mark.timeout(2 * MINUTE, func_only=True)
def test_show_platform_firmware(engines, devices, random_api):
    """Tests nv show platform firmware"""
    with allure.step("Create Platform object"):
        platform = Platform()

    with allure.step("Test output of nv show platform firmware"):
        firmware_items = devices.dut.constants.firmware
        if devices.dut.constants.erots:
            firmware_items = devices.dut.constants.firmware + devices.dut.constants.erots + ['EROT']
        validate_firmware_keys(platform, firmware_items, engines.dut)

    with allure.step("Test specific firmware components"):
        validate_firmware_components(platform, firmware_items, engines.dut, devices.dut)


def validate_firmware_keys(platform, firmware_items, dut_engine):
    all_output = OutputParsingTool.parse_show_output_to_dict(
        platform.firmware.show(dut_engine=dut_engine, output_format=OutputFormat.json),
        output_format=OutputFormat.json, field_name_dict=PlatformConsts.FW_FIELD_NAME_DICT).get_returned_value()
    ValidationTool.validate_set_equal(all_output.keys(), firmware_items)


def validate_firmware_components(platform, firmware_items, engine_dut, device_dut=None, check_gnmi=True):
    if check_gnmi and device_dut is None:
        raise ValueError("device_dut is required when check_gnmi is True")
    errors = {}
    gnmi_client = gnmi_client_for_dut(engine_dut, device_dut) if check_gnmi else None
    for component in firmware_items:
        # WA for the weekend, need to check if it's a bug
        if component == 'BMC' and is_bug_active(4543350):
            break
        # WA --------------------------------------------

        try:
            with allure.step(f"Test output of nv show platform firmware {component}"):
                output = OutputParsingTool.parse_show_output_to_dict(
                    platform.firmware.show(component, dut_engine=engine_dut, output_format=OutputFormat.json),
                    output_format=OutputFormat.json, field_name_dict=PlatformConsts.FW_FIELD_NAME_DICT).get_returned_value()
                if component != 'transceiver':  # Transceiver firmware shows N/A when not specifying a transceiver
                    assert output[PlatformConsts.FW_ACTUAL] not in {'', NvosConst.NOT_AVAILABLE}, \
                        f"{component}.{PlatformConsts.FW_ACTUAL} is empty or N/A"
                with allure.step(f"Compare {component} output against {component} entry in general output"):
                    all_output = OutputParsingTool.parse_show_output_to_dict(
                        platform.firmware.show(dut_engine=engine_dut, output_format=OutputFormat.json),
                        output_format=OutputFormat.json, field_name_dict=PlatformConsts.FW_FIELD_NAME_DICT).get_returned_value()
                    diff = ValidationTool.get_dictionaries_diff(all_output[component], output)
                    assert not diff, (
                        f"The following fields are missing in 'nv show platform firmware {component}' or have a "
                        f"different value compared to 'nv show platform firmware': {diff}"
                    )
                if check_gnmi and (
                    component in {PlatformConsts.FW_ASIC, PlatformConsts.FW_SSD} or
                    component.startswith("EROT-") or
                    component.startswith("SMA")
                ):
                    for gnmi_component in expand_nvue_key_to_gnmi_components(component, device_dut):
                        assert_gnmi_firmware_version_matches_nvue(
                            gnmi_client, gnmi_component, output[PlatformConsts.FW_ACTUAL]
                        )
        except Exception as e:
            errors[component] = e

    assert not errors, f"Test failed for components {list(errors.keys())}. Errors were:\n" + \
        '\n\n'.join(
        f"{component}:\n{type(error).__name__}: {error}" for component, error in errors.items())
