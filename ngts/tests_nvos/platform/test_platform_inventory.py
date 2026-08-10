import logging
import random
from abc import ABC

import pytest
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_constants.constants_nvos import PlatformConsts
from ngts.nvos_constants.constants_nvos import OutputFormat
from ngts.nvos_constants.constants_nvos import ApiType


cumulus_owner = "hiept"


logger = logging.getLogger()


class InventoryItemBaseTest(ABC):
    ITEM_TYPE = ''  # override this in all subclasses

    @classmethod
    def skip_if_needed(cls, devices):
        if not devices.dut.platform_inventory_items_dict.get(cls.ITEM_TYPE):
            pytest.skip(f"Skipping test because DUT has no {cls.ITEM_TYPE}")

    @classmethod
    def choose_item(cls, engines, devices, test_api) -> str:
        item_list = devices.dut.platform_inventory_items_dict[cls.ITEM_TYPE]
        logger.info(f"Will choose randomly from {item_list}")
        ret = random.choice(item_list)
        logger.info(f"Chosen {ret}")
        return ret

    @classmethod
    def validate_fields(cls, engines, devices, test_api, output):
        expected = devices.dut.platform_inventory_values[cls.ITEM_TYPE]
        ValidationTool.validate_output_of_show(output, expected).verify_result()

    @classmethod
    def test_show_item(cls, engines, devices, test_api):
        output_format = OutputFormat.auto if test_api == ApiType.NVUE else OutputFormat.json
        platform = Platform()
        cls.skip_if_needed(devices)

        with allure.step(f"Choosing {cls.ITEM_TYPE} to test"):
            item = cls.choose_item(engines, devices, test_api)

        with allure.step(f"Show and parse info"):
            output = OutputParsingTool.parse_show_output_to_dict(
                platform.inventory.show(item, output_format=output_format),
                output_format=output_format, field_name_dict={'HW version': 'hardware-version'}).get_returned_value()

        with allure.step("Validate fields"):
            cls.validate_fields(engines, devices, test_api, output)


class InventoryPsuTest(InventoryItemBaseTest):
    ITEM_TYPE = 'psu'

    @classmethod
    def choose_item(cls, engines, devices, test_api) -> str:
        platform = Platform()
        output = OutputParsingTool.parse_json_str_to_dictionary(platform.environment.psu.show()).get_returned_value()
        # Filter PSUs where all values are not "N/A"
        psu_list = [key for key, stats in output.items() if all(value != "N/A" for value in stats.values())]
        random_psu = random.choice(psu_list)
        output_format = OutputFormat.auto if test_api == ApiType.NVUE else OutputFormat.json
        output = OutputParsingTool.parse_show_output_to_dict(
            platform.inventory.show(random_psu, output_format=output_format),
            output_format=output_format, field_name_dict={'HW version': 'hardware-version'}).get_returned_value()
        if output.get('state') == 'bad':
            psu_list.remove(random_psu)
            random_psu = random.choice(psu_list)
        return random_psu


class InventoryFanTest(InventoryItemBaseTest):
    ITEM_TYPE = 'fan'

    @classmethod
    def choose_item(cls, engines, devices, test_api) -> str:
        """Choose a fan that is not absent (filter out absent fans)."""
        platform = Platform()
        output_format = OutputFormat.auto if test_api == ApiType.NVUE else OutputFormat.json
        output = OutputParsingTool.parse_show_output_to_dict(
            platform.inventory.show(output_format=output_format),
            output_format=output_format, field_name_dict={'HW Version': 'hardware-version'}).get_returned_value()

        # Filter fans where state is not 'absent'
        fan_list = [fan for fan in devices.dut.platform_inventory_items_dict[cls.ITEM_TYPE]
                    if output.get(fan, {}).get('state') != 'absent']
        logger.info(f"Will choose randomly from present fans: {fan_list}")
        ret = random.choice(fan_list)
        logger.info(f"Chosen {ret}")
        return ret


class InventorySwitchTest(InventoryItemBaseTest):
    ITEM_TYPE = 'switch'


class InventoryBmcTest(InventoryItemBaseTest):
    ITEM_TYPE = 'bmc'


@pytest.mark.platform
@pytest.mark.cumulus
@pytest.mark.nvos_ci
@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.air
def test_show_platform_inventory(engines, devices, test_api):
    """nv show platform inventory"""
    test_classes = [cls for cls in InventoryItemBaseTest.__subclasses__()
                    if devices.dut.platform_inventory_items_dict.get(cls.ITEM_TYPE)
                    ]  # e.g. if the switch has no BMC then don't test for one

    with allure.step("Create System object"):
        platform = Platform()

    with allure.step("Running command and parsing output"):
        output_format = OutputFormat.auto if test_api == ApiType.NVUE else OutputFormat.json
        output = OutputParsingTool.parse_show_output_to_dict(
            platform.inventory.show(output_format=output_format),
            output_format=output_format, field_name_dict={'HW Version': 'hardware-version'}).get_returned_value()

    with allure.step("Checking all inventory items exist"):
        ValidationTool.validate_set_equal(output.keys(), devices.dut.platform_inventory_items).verify_result()

    with allure.step("Checking all fields are present"):
        ValidationTool.validate_set_equal(output[PlatformConsts.HW_COMP_SWITCH].keys(),
                                          devices.dut.platform_inventory_fields).verify_result()

    with allure.step("Determining random sample"):
        sample_items = {test_class: test_class.choose_item(engines, devices, test_api)
                        for test_class in test_classes}
        with allure.step(f"Chosen items: {list(sample_items.values())}"):
            pass  # Shows the list of random samples in allure report

    with allure.step("Checking field values"):
        for test_class, item in sample_items.items():
            with allure.independent_step(f"For {test_class.ITEM_TYPE} {item}"):
                test_class.validate_fields(engines, devices, test_api, output[item])


@pytest.mark.platform
@pytest.mark.cumulus
def test_show_platform_inventory_fan(engines, devices, test_api):
    InventoryFanTest.test_show_item(engines, devices, test_api)


@pytest.mark.platform
@pytest.mark.cumulus
def test_show_platform_inventory_psu(engines, devices, test_api):
    InventoryPsuTest.test_show_item(engines, devices, test_api)


@pytest.mark.platform
@pytest.mark.cumulus
@pytest.mark.air
def test_show_platform_inventory_switch(engines, devices, test_api):
    InventorySwitchTest.test_show_item(engines, devices, test_api)


@pytest.mark.platform
@pytest.mark.cumulus
def test_show_platform_inventory_bmc(engines, devices, test_api):
    InventoryBmcTest.test_show_item(engines, devices, test_api)
