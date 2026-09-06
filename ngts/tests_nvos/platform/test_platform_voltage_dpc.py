import logging
import random
import re
from pathlib import Path

import pytest

from ngts.nvos_constants.constants_nvos import ApiType, HealthConsts, PlatformConsts
from ngts.nvos_tools.Devices.IbDevice import RosalindSwitch
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.infra.DutUtilsTool import RebootParams
from ngts.nvos_tools.infra.Fae import Fae, VoltageDpc
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.ngts_types import DevicesT, EnginesT, TopologyT
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)
DPC_REVISION_PATTERN = re.compile(r'DPC([^\_]+)\_REV([^\_]+)')
SUPPORTED_DEVICES = ["mp29816"]
BAD_DPC_PACKAGE_PATH = Path("/auto/sw_system_project/NVOS_INFRA/verification_files/"
                            "platform_components/DPC/badflow/"
                            "ROSALIND_DPC471_REV0601_DPC472_REV0601_DPC473_REV0601_DPC474_REV0100.tar.gz")


def parse_expected_dpc_revisions(version_name: str) -> dict[str, str]:
    """
    Extract a model-to-revision mapping from the DPC version_name string.

    Example: "DPC471_REV0601_DPC472_REV0601_DPC473_REV0601_DPC474_REV0100"
    Returns: {"0x0471": "0x0601", "0x0472": "0x0601", "0x0473": "0x0601", "0x0474": "0x0100"}
    """
    matches = DPC_REVISION_PATTERN.findall(version_name)
    assert matches, f"No DPC model/revision pairs found in version_name: {version_name}"
    return {f"0x{model.lower():0>4}": f"0x{rev.lower():0>4}" for model, rev in matches}


def verify_dpc_revision_ids(voltage_dpc_obj: VoltageDpc, version_name: str) -> None:
    """
    Verify that the DPC revision IDs shown by 'nv show fae platform voltage-dpc'
    match the expected values encoded in the DPC package version_name.

    Only mp29816 voltmons (PMIC-1 through PMIC-16) are verified;
    comex_voltmon sensors use different device models and are excluded.
    """
    with allure.step(f"Verify DPC revision IDs match expected: {version_name}"):
        expected_revisions = parse_expected_dpc_revisions(version_name)
        logger.info(f"Expected DPC model->revision mapping: {expected_revisions}")

        dpc_output = OutputParsingTool.parse_json_str_to_dictionary(
            voltage_dpc_obj.show()).verify_result()
        logger.info(f"DPC show output: {dpc_output}")

        filtered_sensors = {
            name: data for name, data in dpc_output.items()
            if data["device-name"] in SUPPORTED_DEVICES and
            data["model"] in expected_revisions
        }
        assert filtered_sensors, (
            f"No sensors matched SUPPORTED_DEVICES={SUPPORTED_DEVICES} "
            f"with expected models {list(expected_revisions)}. DPC output: {dpc_output}"
        )

        for sensor_name, sensor_data in filtered_sensors.items():
            assert sensor_data["revision-id"] == expected_revisions[sensor_data["model"]], (
                f"Sensor {sensor_name}: Model={sensor_data['model']}, "
                f"expected Revision ID={expected_revisions[sensor_data['model']]}, got {sensor_data['revision-id']}"
            )
            logger.info(f"Sensor {sensor_name}: Model={sensor_data['model']}, Revision ID={sensor_data['revision-id']} - OK")


def _dpc_fetch_install_and_verify(voltage_dpc_obj: VoltageDpc, path: str, filename: str,
                                  version_name: str, topology_obj: TopologyT,
                                  expect_install_failure: bool = False) -> None:
    """
    Generic DPC package install cycle: fetch, verify file listed, install with
    reboot recovery, and verify revision IDs.

    When expect_install_failure is True the install is expected to fail (no reboot,
    no revision-ID verification).
    """
    with allure.step(f"Fetch DPC image: {filename}"):
        voltage_dpc_obj.action_fetch(path).verify_result()

    with allure.step("Assert fetched file appears in files list"):
        voltage_dpc_obj.files.verify_show_files_output(expected_files=[filename])

    if expect_install_failure:
        with allure.step(f"Install bad DPC version (expected to fail): {version_name}"):
            voltage_dpc_obj.files.file_name[filename].action_install(
                reboot_params=False,
                force=False,
            ).verify_result(should_succeed=False)
    else:
        with allure.step(f"Install DPC version: {version_name}"):
            voltage_dpc_obj.files.file_name[filename].action_install(
                reboot_params=RebootParams(topology_obj=topology_obj),
                force=False,
            ).verify_result()

        verify_dpc_revision_ids(voltage_dpc_obj, version_name)

    with allure.step("Validate system health is OK after install"):
        System().validate_health_status(HealthConsts.OK)


@pytest.mark.dut_reboot
@pytest.mark.parametrize("test_api", [random.choice(ApiType.ALL_TYPES)])
def test_voltage_dpc_install(engines: EnginesT, devices: DevicesT, topology_obj: TopologyT, test_api: ApiType):
    """
    Test voltage-dpc update via fae platform.

    Commands under test:
        nv show fae platform voltage-dpc
        nv action fetch fae platform voltage-dpc <remote-url>
        nv show fae platform voltage-dpc files
        nv action install fae platform voltage-dpc <file-name>

    Test flow:
        1. Skip if device is not Rosalind
        2. Fetch and install previous DPC version (triggers reboot)
        3. Verify revision IDs match the previous version
        4. Restore latest DPC version (triggers reboot)
        5. Verify revision IDs match the latest version
        6. Clean up fetched files
    """
    if not isinstance(devices.dut, RosalindSwitch):
        pytest.skip("DPC voltage update is only supported on Rosalind devices")

    TestToolkit.tested_api = test_api
    voltage_dpc_obj = Fae().platform.voltage_dpc

    with allure.step("Get DPC version info from JSON"):
        previous_info = BmcTool.get_fw_component_version_dict(PlatformConsts.DPC, "previous")
        latest_info = BmcTool.get_fw_component_version_dict(PlatformConsts.DPC, "latest")

    try:
        with allure.step("Install previous DPC version"):
            _dpc_fetch_install_and_verify(
                voltage_dpc_obj,
                previous_info['path'],
                previous_info['filename'],
                previous_info['version_name'],
                topology_obj,
            )
    finally:
        with allure.step("Install latest DPC version"):
            _dpc_fetch_install_and_verify(
                voltage_dpc_obj,
                latest_info['path'],
                latest_info['filename'],
                latest_info['version_name'],
                topology_obj,
            )

        with allure.step("Clean up fetched DPC files"):
            voltage_dpc_obj.files.delete_all_existing_files()


@pytest.mark.parametrize("test_api", [random.choice(ApiType.ALL_TYPES)])
def test_voltage_dpc_install_bad_flow(engines: EnginesT, devices: DevicesT, topology_obj: TopologyT, test_api: ApiType):
    """
    Test that installing a bad DPC package fails gracefully.

    Commands under test:
        nv action fetch fae platform voltage-dpc <remote-url>
        nv show fae platform voltage-dpc files
        nv action install fae platform voltage-dpc <file-name>

    Test flow:
        1. Skip if device is not Rosalind
        2. Fetch a known-bad DPC package (fetch succeeds)
        3. Attempt install -- expect failure, no reboot
        4. Clean up fetched files
    """
    if not isinstance(devices.dut, RosalindSwitch):
        pytest.skip("DPC voltage update is only supported on Rosalind devices")

    TestToolkit.tested_api = test_api
    voltage_dpc_obj = Fae().platform.voltage_dpc

    filename = BAD_DPC_PACKAGE_PATH.name
    version_name = Path(BAD_DPC_PACKAGE_PATH.stem).stem.replace("ROSALIND_", "")

    try:
        _dpc_fetch_install_and_verify(
            voltage_dpc_obj,
            str(BAD_DPC_PACKAGE_PATH),
            filename,
            version_name,
            topology_obj,
            expect_install_failure=True,
        )
    finally:
        with allure.step("Clean up fetched DPC files"):
            voltage_dpc_obj.files.delete_all_existing_files()
