import logging
import random
import json
import re
import pytest
from datetime import datetime


cumulus_owner = "mpetchimuthu"


logger = logging.getLogger()
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.mark.version
@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_show_system_version(test_api, engines, devices, nv_command):
    """
    Run show system version command and verify version values
        Test flow
        1. run show system version
        2. validate values in db
    """
    TestToolkit.tested_api = test_api

    version_output, version_image_output = _verify_system_show_version_sanity(devices, nv_command)

    with allure.step(f"Verify product-release is equal to build-id"):
        product_release = version_output[SystemConsts.VERSION_PRODUCT_RELEASE]
        build_id = version_image_output[SystemConsts.VERSION_BUILD_ID]
        assert product_release in build_id, f"Product release '{product_release}' not found in build-id '{build_id}'"


@pytest.mark.nvos_chipsim_ci
def test_show_system_version_sanity(devices, nv_command):
    _verify_system_show_version_sanity(devices, nv_command)


def _verify_system_show_version_sanity(devices, nv_command):
    with allure.step('Run show system command and verify that each field has a value'):
        version_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.version.show()).get_returned_value()

        version_image_output = OutputParsingTool.parse_json_str_to_dictionary(
            nv_command.system.version.image.show()).get_returned_value()

        ValidationTool.verify_all_fields_value_exist_in_output_dictionary(
            version_output, nv_command.system.get_expected_fields(devices.dut, 'version')).verify_result()

        return version_output, version_image_output


@pytest.mark.nvos_chipsim_ci
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_show_system_version_sanity(test_api, devices, nv_command):
    TestToolkit.tested_api = test_api
    _verify_system_show_version_sanity(devices, nv_command)


def _verify_system_show_version_sanity(devices, nv_command):
    with allure.step('Run show system command and verify that each field has a value'):
        version_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.version.show()).get_returned_value()

        version_image_output = OutputParsingTool.parse_json_str_to_dictionary(
            nv_command.system.version.image.show()).get_returned_value()

        ValidationTool.verify_all_fields_value_exist_in_output_dictionary(
            version_output, nv_command.system.get_expected_fields(devices.dut, 'version')).verify_result()

        return version_output, version_image_output


@pytest.mark.version
@pytest.mark.cumulus
@pytest.mark.system
def test_show_system_version_image(test_api, engines, devices, nv_command):
    """
    Test 'nv show system version image' command output
    """
    with allure.step("Run show command to view system version image"):
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.version.image.show()).get_returned_value()
        expected_fields = [SystemConsts.VERSION_BUILD_ID, SystemConsts.VERSION_BUILD_DATE]
        ValidationTool.verify_all_fields_value_exist_in_output_dictionary(output_dictionary, expected_fields).verify_result()


@pytest.mark.version
@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_show_system_version_packages(engines, test_api, output_format, nv_command):
    """nv show system version packages"""
    TestToolkit.tested_api = test_api

    with allure.step("Check show software output"):
        output = OutputParsingTool.parse_show_output_to_dict(nv_command.system.version.packages.show(output_format=output_format),
                                                             output_format=output_format).get_returned_value()
        assert output, f"'nv show system version packages' returned empty output"
        if test_api == ApiType.OPENAPI:
            installed_str = "installed"
            ValidationTool.validate_set_equal(output.keys(), {installed_str}).verify_result()
            output = output[installed_str]
        ValidationTool.validate_set_equal(tuple(output.values())[0].keys(), SystemConsts.SW_FIELD_NAMES
                                          ).verify_result()


@pytest.mark.version
@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_show_system_version_packages_installed(engines, test_api, output_format, nv_command):
    """`nv show system version packages installed` and `nv show system version packages installed <software-id>`"""
    TestToolkit.tested_api = test_api

    with allure.step("Check show software installed output"):
        output = OutputParsingTool.parse_show_output_to_dict(
            nv_command.system.version.packages.installed.show(output_format=output_format),
            output_format=output_format).get_returned_value()
        assert output, f"'nv show system version packages installed' returned empty output"
        ValidationTool.validate_set_equal(tuple(output.values())[0].keys(), SystemConsts.SW_FIELD_NAMES
                                          ).verify_result()

    with allure.step("Verify output for a specific SW"):
        random_software = random.choice(tuple(output.keys()))
        logging.info(f"Verify fields for {random_software}")
        specific_output = OutputParsingTool.parse_show_output_to_dict(
            nv_command.system.version.packages.installed.show(op_param=random_software, output_format=output_format),
            output_format=output_format).get_returned_value()
        ValidationTool.validate_set_equal(specific_output.keys(), SystemConsts.SW_FIELD_NAMES).verify_result()


@pytest.mark.version
@pytest.mark.system
@pytest.mark.cumulus_only
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_show_system_info(test_api, engines, devices, nv_command):
    """
    Verify system information using 'nv show system' command

    Test flow:
    1. Execute 'nv show system' command
    2. Verify system information including:
       - Hostname
       - Version
       - Build information
       - System state
    3. Validate all system parameters are correct
    """
    TestToolkit.tested_api = test_api

    with allure.step('Get system information from NVUE'):
        system_out = OutputParsingTool.parse_json_str_to_dictionary(
            nv_command.system.show()).get_returned_value()
        logger.info(f"NVUE system output: {json.dumps(system_out, indent=2)}")

    with allure.step('Validate hostname consistency'):
        nvue_hostname = system_out.get('hostname')
        system_hostname = devices.dut.hostname
        logger.info(f"NVUE hostname: {nvue_hostname}")
        logger.info(f"System hostname: {system_hostname}")
        assert nvue_hostname is not None, "Hostname is missing in NVUE system information"
        assert nvue_hostname == system_hostname, f"Hostname mismatch - NVUE: {nvue_hostname}, System: {system_hostname}"

    with allure.step('Get and parse /etc/image-release'):
        image_release_out = devices.dut.run_cmd("cat /etc/image-release")
        image_release_dict = {}
        for line in image_release_out.splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                image_release_dict[key] = value.strip('"')
        logger.info(f"Parsed /etc/image-release: {json.dumps(image_release_dict, indent=2)}")

    with allure.step('Get and parse /etc/lsb-release'):
        lsb_release_out = devices.dut.run_cmd("cat /etc/lsb-release")
        lsb_release_dict = {}
        for line in lsb_release_out.splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                lsb_release_dict[key] = value.strip('"')
        logger.info(f"Parsed /etc/lsb-release: {json.dumps(lsb_release_dict, indent=2)}")

    with allure.step('Validate system version information'):
        # Get version information
        version_output = OutputParsingTool.parse_json_str_to_dictionary(
            nv_command.system.version.show()).get_returned_value()
        version_image_output = OutputParsingTool.parse_json_str_to_dictionary(
            nv_command.system.version.image.show()).get_returned_value()

        # Validate product release consistency
        nvue_release = version_output.get(SystemConsts.VERSION_PRODUCT_RELEASE)
        image_release = image_release_dict.get('VERSION')
        lsb_release = lsb_release_dict.get('DISTRIB_RELEASE')

        assert nvue_release == image_release, f"Product release mismatch - NVUE: {nvue_release}, image-release: {image_release}"
        assert nvue_release == lsb_release, f"Product release mismatch - NVUE: {nvue_release}, lsb-release: {lsb_release}"

        # Validate build ID format and consistency
        build_id = version_image_output.get(SystemConsts.VERSION_BUILD_ID)
        assert build_id, "Build ID is missing in version information"
        assert nvue_release in build_id, f"Product release {nvue_release} not found in build ID {build_id}"

        # Validate build date format
        build_date = version_image_output.get(SystemConsts.VERSION_BUILD_DATE)
        assert build_date, "Build date is missing in version information"
        try:
            datetime.strptime(build_date, "%a %b %d %H:%M:%S %Z %Y")
        except ValueError as e:
            assert False, f"Invalid build date format: {build_date}. Expected format: 'Day Mon DD HH:MM:SS TZ YYYY'"
