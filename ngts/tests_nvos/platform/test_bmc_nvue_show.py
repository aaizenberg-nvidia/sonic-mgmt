"""
P0 NVUE BMC Show Command Tests (TC-BMC-001 through TC-BMC-004).

Validates all 5 NVUE BMC show commands produce correct, non-empty output
with valid JSON. Designed for bmc-cli reuse via the ``interface`` parameter
pattern in bmc_helpers.
"""
import logging

import pytest

from ngts.nvos_constants.constants_nvos import PlatformConsts, FansConsts
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.platform.bmc_helpers import (
    fetch_bmc_firmware,
    get_bmc_firmware,
    get_bmc_inventory,
    get_fw_info,
    skip_if_no_bmc,
)
from ngts.tools.test_utils import allure_utils as allure

cumulus_owner = "gpazbenitzha"
logger = logging.getLogger()

BMC_INVENTORY_MIN_FIELDS = 3


@pytest.mark.bmc
@pytest.mark.platform
@pytest.mark.skip_clear_config
@pytest.mark.cumulus
def test_nvue_show_platform_inventory_bmc(engines, devices, topology_obj):
    """TC-BMC-001: Verify NVUE inventory commands return BMC info with state 'ok'.

    Steps:
        1. ``Platform().inventory.show()`` -- verify BMC appears as a key.
        2. ``Platform().inventory.show("BMC")`` -- verify state is 'ok'.
        3. Verify inventory has a minimum number of populated fields.
        4. Verify all returned fields are non-empty.
        5. Cross-check: BMC entry from full listing matches BMC-specific query.
    """
    skip_if_no_bmc(devices, engines)
    platform = Platform()

    with allure.step("Verify BMC appears in full platform inventory"):
        full_inventory = OutputParsingTool.parse_json_str_to_dictionary(
            platform.inventory.show()).get_returned_value()
        assert 'BMC' in full_inventory, \
            (f"BMC missing from platform inventory: "
             f"{list(full_inventory.keys())}")

    with allure.step("Get BMC inventory and verify state is 'ok'"):
        bmc_inventory = get_bmc_inventory(engines)
        assert bmc_inventory, "BMC inventory returned empty"
        bmc_state = bmc_inventory.get(PlatformConsts.INV_STATE)
        assert bmc_state == FansConsts.STATE_OK, \
            f"BMC state is '{bmc_state}', expected '{FansConsts.STATE_OK}'"

    with allure.step(
            f"Verify inventory has at least {BMC_INVENTORY_MIN_FIELDS} fields"):
        assert len(bmc_inventory) >= BMC_INVENTORY_MIN_FIELDS, \
            (f"BMC inventory has only {len(bmc_inventory)} fields "
             f"(expected >= {BMC_INVENTORY_MIN_FIELDS}): "
             f"{list(bmc_inventory.keys())}")

    with allure.step("Verify all returned fields are non-empty"):
        # Intentional product contract: any key NVUE returns for BMC inventory
        # must carry a real value. Empty/null fields are treated as a bug
        # (including newly added keys that land blank).
        empty_fields = [k for k, v in bmc_inventory.items()
                        if v is None or str(v).strip() == '']
        assert not empty_fields, \
            f"BMC inventory has empty fields: {empty_fields}"

    with allure.step(
            "Cross-check: BMC from full listing matches BMC-specific query"):
        bmc_from_full = full_inventory.get('BMC', {})
        full_state = bmc_from_full.get(PlatformConsts.INV_STATE)
        assert full_state == bmc_inventory.get(PlatformConsts.INV_STATE), \
            (f"State mismatch: full listing reports '{full_state}', "
             f"BMC-specific reports '{bmc_state}'")


@pytest.mark.bmc
@pytest.mark.platform
@pytest.mark.skip_clear_config
@pytest.mark.cumulus
def test_nvue_show_platform_firmware_bmc(engines, devices, topology_obj):
    """TC-BMC-002: Verify NVUE firmware commands return BMC firmware version.

    Steps:
        1. ``Platform().firmware.show()`` -- verify BMC component appears.
        2. ``Platform().firmware.show("BMC")`` -- verify basic firmware fields
           present.
        3. Validate actual-firmware is a meaningful version string
           (not empty/placeholder).
        4. Cross-check: BMC firmware version from full listing matches
           BMC-specific query.
    """
    skip_if_no_bmc(devices, engines)
    platform = Platform()

    with allure.step("Verify BMC appears in full firmware listing"):
        full_firmware = OutputParsingTool.parse_json_str_to_dictionary(
            platform.firmware.show()).get_returned_value()
        assert 'BMC' in full_firmware, \
            (f"BMC missing from firmware listing: "
             f"{list(full_firmware.keys())}")

    with allure.step("Get BMC firmware and verify basic fields present"):
        bmc_firmware = get_bmc_firmware(engines)
        assert bmc_firmware, "BMC firmware info returned empty"
        for field in PlatformConsts.FW_BASIC_FIELDS:
            assert field in bmc_firmware, \
                (f"Required field '{field}' missing from BMC firmware: "
                 f"{list(bmc_firmware.keys())}")

    with allure.step("Validate actual-firmware is a meaningful version string"):
        actual_fw = bmc_firmware[PlatformConsts.FW_ACTUAL]
        assert isinstance(actual_fw, str) and len(actual_fw.strip()) > 0, \
            f"actual-firmware is empty or not a string: '{actual_fw}'"
        placeholder_values = {'', 'N/A', 'unknown', 'PLACEHOLDER', 'none'}
        assert actual_fw.strip().lower() not in placeholder_values, \
            f"actual-firmware appears to be a placeholder: '{actual_fw}'"
        logger.info(f"BMC firmware version: {actual_fw}")

    with allure.step(
            "Cross-check: firmware version from full listing matches "
            "BMC-specific"):
        bmc_from_full = full_firmware.get('BMC', {})
        full_version = bmc_from_full.get(PlatformConsts.FW_ACTUAL)
        assert full_version == actual_fw, \
            (f"Version mismatch: full listing reports '{full_version}', "
             f"BMC-specific reports '{actual_fw}'")


@pytest.mark.bmc
@pytest.mark.platform
@pytest.mark.skip_clear_config
@pytest.mark.cumulus
def test_nvue_show_platform_firmware_bmc_files(
        engines, devices, topology_obj):
    """TC-BMC-003: Verify firmware files listing after a fetch.

    Command under test (CL 5.19): ``nv show platform firmware BMC files``.
    Fetch uses ``nv action fetch platform firmware BMC <remote-url>`` via
    ``fetch_bmc_firmware`` (on-switch / file://). Do not install.

    The listing key is the filename. The ``path`` field is the last fetch
    source (e.g. ``/tmp/<file>`` after file://), not a fixed staging dir.
    ``nv action delete platform firmware BMC files`` is not on 5.19.
    """
    skip_if_no_bmc(devices, engines)
    platform = Platform()
    bmc_component = platform.firmware.bmc

    with allure.step("Get firmware path from FW JSON"):
        path, filename, version_name, base_url = get_fw_info(
            engines.dut, "latest")
        logger.info(
            f"Fetching BMC firmware: {filename} from {path} "
            f"(version={version_name}, base_url={base_url})")

    with allure.step("Fetch BMC firmware image (no install)"):
        fetch_bmc_firmware(path, base_url=base_url)

    with allure.step("Verify fetched file appears in files listing"):
        files = bmc_component.files.get_files()
        assert files, "Firmware files listing is empty after fetch"
        assert filename in files, \
            (f"Fetched file '{filename}' not found in files listing: "
             f"{list(files.keys())}")

    with allure.step("Verify fetched file entry has content"):
        file_entry = files[filename]
        logger.info(f"File entry for '{filename}': {file_entry}")
        assert isinstance(file_entry, dict) and file_entry, \
            f"File entry for '{filename}' is empty: {file_entry!r}"
        staged_path = file_entry.get("path")
        if staged_path is not None:
            assert filename in str(staged_path), \
                f"path does not contain filename: {staged_path}"

    with allure.step("Verify files listing JSON is valid and non-empty"):
        files_output = bmc_component.files.show()
        parsed = OutputParsingTool.parse_json_str_to_dictionary(
            files_output).get_returned_value()
        assert parsed, "Files JSON output parsed to empty result"
        assert filename in parsed, \
            (f"Fetched file '{filename}' not in JSON output: "
             f"{list(parsed.keys())}")
