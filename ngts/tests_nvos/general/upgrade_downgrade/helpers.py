from __future__ import annotations

from typing import Iterator, Any
from pathlib import Path
import dataclasses
import logging
import json
import enum
import yaml
import os

from ngts.tests_nvos.general.security.security_test_tools.constants import AaaConsts
from ngts.tests_nvos.general.security.password_hardening.PwhConsts import PwhConsts
from ngts.scripts.sonic_deploy.nvos_only_methods import NvosInstallationSteps
from ngts.nvos_constants.constants_nvos import ImageConsts, SystemConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import PlatformConsts
from ngts.nvos_tools.system.Firmware import PlatformComponent
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.ngts_types import EnginesT, DevicesT
from ngts.nvos_tools.system.Asic import Asic

logger = logging.getLogger(__name__)
ResultsMetadataT = dict['ResultMetadata', Any | dict[str, Any]]


class ResultMetadata(enum.IntEnum):
    """
        Metadata keys to help pass data from operations results to checkers.

        e.g. if there was a FW update, checker would know that the reboot reason should be 'power cycle'.
    """
    HAD_FW_UPDATE = enum.auto()
    DURATION_ERROR = enum.auto()
    FEATURE_CHECKER_ERROR = enum.auto()


class Action(enum.IntEnum):
    """ The action to perform. """
    UPGRADE = enum.auto()
    UPGRADE_NO_CONFIG_FILE = enum.auto()
    DOWNGRADE = enum.auto()
    ROLLBACK = enum.auto()

    @property
    def is_upgrade(self) -> bool:
        return self in (self.UPGRADE, self.UPGRADE_NO_CONFIG_FILE)


class FWInstallState(enum.IntEnum):
    """ The state of the firmware installation. """
    NA = enum.auto()
    FETCH = enum.auto()
    INSTALL = enum.auto()
    VERIFY = enum.auto()

    def __str__(self):
        return self.name.title()


@dataclasses.dataclass
class SystemImage:
    """ The system images and partitions that results from the command `nv show system image` """
    current: str
    other: str
    current_partition: str
    other_partition: str

    def __iter__(self) -> Iterator[str]:
        return iter((self.current, self.other))


@dataclasses.dataclass
class SystemPackage:
    """ System package. """
    name: str
    nvos: Path
    recipe: Path
    config: Path | None = None

    @staticmethod
    def _resolve(path: str) -> Path:
        """ Resolve the path to the file. """
        if not path:
            raise ValueError(f"Path {path!r} is empty")

        try:
            p = Path(path)
            return next(Path(p.parent).glob(p.name))
        except StopIteration:
            raise FileNotFoundError(f"File {path!r} not found")

    @classmethod
    def from_dict(cls, data: dict[str, Any], name: str) -> 'SystemPackage':
        nvos = cls._resolve(data['nvos'])  # type: ignore
        recipe = cls._resolve(data['recipe'])  # type: ignore

        config: Path | str | None = data.get('config')
        if config:
            if os.path.dirname(config) != '':  # if the config is a full path, use it as is
                config = cls._resolve(config)  # type: ignore
            elif (NvosConst.DEFAULT_NVOS_CONFIG_PATH / config).exists():
                config = NvosConst.DEFAULT_NVOS_CONFIG_PATH / config
            else:
                raise FileNotFoundError(f"Config file {config} not found")

        return cls(name=name, nvos=nvos, recipe=recipe, config=config)


@dataclasses.dataclass
class SystemVersionTransition:
    """ System version transition. """
    base: SystemPackage
    target: SystemPackage
    action: Action = Action.UPGRADE
    skip_rollback_cleanup: bool = dataclasses.field(default=False, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'SystemVersionTransition':
        if isinstance(data['action'], str):
            action = Action[data['action'].upper()]
        base = SystemPackage.from_dict(data['base'], name='base')  # type: ignore
        target = SystemPackage.from_dict(data['target'], name='target')  # type: ignore

        return cls(base=base, target=target, action=action)


@dataclasses.dataclass(eq=False, frozen=True)
class Result:
    """ Result of an operation. """
    ok: bool
    operation: str
    error_message: str | None = None
    skipped: bool = False
    duration_error: Any | None = None

    def __str__(self) -> str:
        if self.ok:
            return self.operation
        return f'{self.operation} => {self.error_message}'

    def __bool__(self):
        return self.ok


def _get_expected_fw_versions(firmware: list[str], recipe: dict[str, Any], single_pkg: bool = False) -> dict[str, str]:
    """Build expected platform firmware versions from the recipe."""
    if single_pkg:
        expected_fw_versions = recipe['single_package']['latest'].get('version_name')
        if not isinstance(expected_fw_versions, dict):
            raise ValueError("recipe single_package.latest.version_name must be a dictionary")
        return expected_fw_versions

    expected_fw_versions: dict[str, str] = {}
    for item in firmware:
        if item == 'ASIC':
            continue  # The ASIC comes with the NVOS, so we don't need to verify it

        if item == 'SSD':
            continue  # SSD is managed by the NVOS and verified by feature checker

        if item.startswith('CPLD'):
            if 'cpld' in recipe:
                expected_fw_versions[item] = recipe['cpld']['latest']['version_name'][item]
        else:
            if (item_lower := item.lower()) in recipe:
                if item_lower == 'fpga' and _is_encrypted_fpga():
                    item_lower += '_encrypted'

                expected_fw_versions[item] = recipe[item_lower]['latest']['version_name']

    return expected_fw_versions


def _verify_fw_versions(
    firmware: list[str],
    recipe: dict[str, Any],
    current_fw_versions: dict[str, str | None],
    single_pkg: bool = False,
) -> Result:
    """
    Verify the firmware versions.

    Args:
        firmware: list of firmware components to update.
        recipe: Recipe dictionary containing the firmware versions.
        current_fw_versions: Current firmware versions.
        single_pkg: If True, get expected versions from recipe['single_package']['latest'].

    Returns:
        Result: Result of the firmware version verification.
    """
    expected_fw_versions = _get_expected_fw_versions(firmware, recipe, single_pkg=single_pkg)
    errors = {k: f'cur:{v} != exp:{expected_fw_versions[k]}' for k, v in current_fw_versions.items() if k in expected_fw_versions and v != expected_fw_versions[k]}
    return Result(ok=not errors, operation="FW versions", error_message="FW versions are not as expected: %s" % errors)


def _platform_fw_versions_changed(before: dict[str, str | None], after: dict[str, str | None]) -> bool:
    """ Return True if any reported platform firmware version changed. """
    return any(before.get(component) != after.get(component) for component in before.keys() | after.keys())


def _update_cpld(recipe: dict[str, dict[str, Any] | str], scp_path: str, current_fw_versions: dict[str, Any], devices=None) -> list[Result]:
    """
    Update the CPLD FW.

    Args:
        recipe: Recipe dictionary containing the firmware versions.
        scp_path: Path to the SCP server.
        current_fw_versions: Current firmware versions.
        devices: Devices object for threshold lookup.

    Returns:
        list[Result]: list of results of the CPLD FW update.
    """
    state = FWInstallState.NA
    if 'cpld' not in recipe:
        return []

    platform = Platform()
    recipe_component: dict[str, str] = recipe['cpld']['latest']
    target_fw_version = recipe_component['version_name']

    with allure.step((allure_step := "Update CPLD FW") + f' -> {target_fw_version}'):
        component = platform.firmware.cpld

        try:
            with allure.step("Get Current FW CPLD version"):
                current_cpld_fw = {k: v for k, v in current_fw_versions.items() if k.startswith("CPLD")}
                if current_cpld_fw == recipe_component['version_name']:
                    logger.info("CPLD FW is up to date... skipping...")
                    return []

            with allure.step("Fetch CPLD FW"):
                state = FWInstallState.FETCH
                component.action_fetch(recipe_component['path'], base_url=scp_path).verify_result()

            with allure.step("Install CPLD FW"):
                state = FWInstallState.INSTALL
                filename = recipe_component.get('filename', Path(recipe_component['path']).name)
                res_obj = BmcTool.install_fw_image_without_reboot(
                    platform_component=component,
                    test_name='test_change_nvos_version',
                    filename=filename,
                )

            try:  # We don't want to fail the test if the duration is not as expected at this stage.
                with allure.step("Verify Operation Time"):
                    OperationTime.verify_operation_time(res_obj.duration, 'cpld install without reboot', devices).verify_result()
            except AssertionError as e:
                logger.exception(e)
                return [Result(ok=True, operation=f'CPLD FW {state}', duration_error=str(e))]
            return [Result(ok=True, operation=f'CPLD FW {state}')]

        except Exception as e:
            logger.error(f"Failed to {state} CPLD FW: {e}")
            allure.attach(allure_step, f'CPLD FW {state} failed: {e}')
            return [Result(ok=False, operation=f'CPLD FW {state}', error_message=str(e))]


def _is_encrypted_fpga() -> bool:
    with allure.step("Check if FPGA is encrypted"):
        fpga_ver = Platform().parse_show()[PlatformConsts.FW_PART_NUMBER].strip()
        non_encrypted_fpga_ips = (
            "692-9K36F-A5MV-JS0",
            "692-9K36F-00MV-JSL",
            "920-9K36F-00MV-ES1",
        )
        return fpga_ver not in non_encrypted_fpga_ips


def _fetch_n_update_fw(firmware: list[str], recipe: dict[str, dict[str, Any] | str], scp_path: str, current_fw_versions: dict[str, str], devices=None) -> list[Result]:
    '''
    Fetch and update the firmware of the given components.

    Args:
        firmware: list of firmware components to update.
        recipe: Recipe dictionary containing the firmware versions.
        scp_path: Path to the SCP server.
        devices: Devices object for threshold lookup.

    Returns:
        Dictionary containing the results of the firmware update for each component.
    '''
    platform = Platform()
    results: list[Result] = []

    for component_name in map(str.lower, firmware):
        if component_name not in recipe:
            logger.info(f"Component {component_name} not found in recipe")
            continue
        if component_name.startswith('asic'):
            logger.info("Skipping ASIC - managed by the NVOS")
            continue
        if component_name.startswith('cpld'):  # will be handled separately
            continue
        if component_name.startswith('ssd'):
            logger.info("Skipping SSD - managed by the NVOS")
            continue

        state = FWInstallState.NA
        component: PlatformComponent | Asic | None = getattr(platform.firmware, component_name, None)
        if component_name == 'fpga' and _is_encrypted_fpga():
            component_name += '_encrypted'

        recipe_component = recipe[component_name]['latest']
        filename = recipe_component.get('filename', Path(recipe_component['path']).name)
        logger.debug(f'{filename=}')

        with allure.step("Store Current and Target FW versions"):
            target_fw_version = recipe_component['version_name']
            if component_name == 'fpga_encrypted':
                current_fw_version = current_fw_versions['FPGA']
            else:
                current_fw_version = current_fw_versions[component_name.upper()]

        with allure.step(allure_step := f"Update {component_name} {current_fw_version!r} -> {target_fw_version!r} FW"):
            with allure.step(f"Compare Current {component_name} {current_fw_version!r} FW version against recipe version {filename!r}"):
                if current_fw_version == target_fw_version:
                    logger.info(f"{component_name} FW is up to date... skipping...")
                    continue

            try:
                with allure.step(f"Fetch {component_name} {target_fw_version!r} FW"):
                    state = FWInstallState.FETCH
                    component.action_fetch(recipe_component['path'], base_url=scp_path).verify_result()

                with allure.step(f"Install {component_name} {target_fw_version!r} FW"):
                    state = FWInstallState.INSTALL

                    (res_obj := BmcTool.install_fw_image_without_reboot(
                        platform_component=component,
                        test_name='test_change_nvos_version',
                        filename=filename,
                    )).verify_result()

                try:  # We don't want to fail the test if the duration is not as expected at this stage.
                    with allure.step(f"Verify {component_name} FW install without reboot Operation Time ({res_obj.duration}s)"):
                        logger.debug(f'{component_name} FW install without reboot Operation Time: {res_obj.duration}')
                        OperationTime.verify_operation_time(
                            res_obj.duration,
                            f'{component_name.replace("_encrypted", "")} install without reboot',  # case of encrypted FPGA. only keep the name.
                            devices,
                        ).verify_result()
                    results.append(Result(ok=True, operation=f'{component_name} FW {state}'))
                except AssertionError as e:
                    logger.exception(e)
                    results.append(Result(ok=True, operation=f'{component_name} FW {state}', duration_error=str(e)))

            except Exception as e:
                error = e if isinstance(e, AssertionError) else f"Failed to {state} {component_name} FW: {e}"
                logger.error(error)
                allure.attach(allure_step, f'{component_name} FW {state} failed: {e}')
                results.append(Result(ok=False, operation=f'{component_name} FW {state}', error_message=str(e)))

    return results


def _get_single_platform_fw_recipe(recipe: dict[str, dict | list | str]) -> dict[str, Any] | None:
    """Return recipe metadata for the aggregate platform firmware package."""
    return recipe.get('single_package', {}).get('latest')


def _fetch_n_update_single_platform_fw(recipe_component: dict[str, Any], scp_path: str) -> list[Result]:
    """
    Fetch and install a single platform firmware package without reboot.

    The following NVOS image install remains the reboot boundary for matrix tests.
    """

    platform = Platform()

    state = FWInstallState.NA
    component = platform.firmware

    package_path = recipe_component['path']
    filename = recipe_component.get('filename', Path(package_path).name)
    logger.debug(f'{filename=}')

    with allure.step(allure_step := f"Update platform FW with single package {filename!r}"):
        try:
            with allure.step("Fetch platform firmware package"):
                state = FWInstallState.FETCH
                component.action_fetch(package_path, base_url=scp_path).verify_result()
                component.files.verify_show_files_output(expected_files=[filename])

            with allure.step("Install platform firmware package without reboot"):
                state = FWInstallState.INSTALL
                (res_obj := BmcTool.install_fw_image_without_reboot(
                    platform_component=component,
                    test_name='test_change_nvos_version',
                    filename=filename,
                )).verify_result()

            # TODO: check if we need to add verification of the duration of the install operation.
            return [Result(ok=True, operation=f'Platform FW {state}')]

        except Exception as e:
            logger.exception(e)
            allure.attach(allure_step, f'Platform FW package {state} failed: {e}')
            return [Result(ok=False, operation=f'Platform FW package {state}', error_message=str(e))]


def _load_recipe(sys_pkg: SystemPackage, provisioning: str) -> dict[str, Any]:
    """Load the package recipe for the current provisioning mode."""
    if not sys_pkg.recipe:
        raise ValueError(f"{sys_pkg.name} package must include 'recipe'")

    with allure.step("Load recipe file"):
        with open(sys_pkg.recipe) as reader:
            return json.load(reader)[provisioning]


def _update_nvos(engines: EnginesT, topology_obj, nvos: Path) -> Result:
    '''
    Update the NVOS of the given engine.

    Args:
        engines: Engines object containing the engine to update.
        nvos: Path to the NVOS image.

    Returns:
        True if the NVOS was updated successfully, False otherwise.
    '''
    system = System()
    with allure.step("Check Current NVOS"):
        sys_image = get_system_image()
        if sys_image.current == nvos.name.replace('-amd64', '').replace('.bin', ''):
            logger.info("Current NVOS is the same as the target NVOS, skipping NVOS update")
            return Result(ok=True, operation="NVOS", skipped=True)

    with allure.step("Fetch NVOS"):
        system.image.action_fetch(
            ImageConsts.SCP_PATH_SERVER.format(
                username=engines.sonic_mgmt.username,
                password=engines.sonic_mgmt.password,
                ip=IpTool.format_ip_for_uri(engines.sonic_mgmt),
                path=nvos,
            ),
            base_url="",
        )

    with allure.step("Install NVOS"):
        res_obj, _ = OperationTime.save_duration(
            'nvos install with reboot',
            '',
            'test_change_nvos_version',
            system.image.files.file_name[nvos.name].action_file_install_with_reboot,
            topology_obj=topology_obj,
        )
        logger.debug(f"NVOS install with reboot Operation Time took: {res_obj.duration}")
        res_obj.verify_result()

    try:  # We don't want to fail the test if the duration is not as expected at this stage.
        duration_error = None
        with allure.step("Verify NVOS install with reboot Operation Time"):
            OperationTime.verify_operation_time(
                res_obj.duration,
                'install nvos',
                threshold=700
            ).verify_result()
    except AssertionError as e:
        duration_error = str(e)
        logger.exception(e)

    with allure.step("Verify NVOS"):
        sys_image = get_system_image()
        return Result(ok=sys_image.current == nvos.name.replace('-amd64', '').replace('.bin', ''), operation="NVOS", duration_error=duration_error)


def update_system_fw(devices: DevicesT, engines: EnginesT, sys_pkg: SystemPackage, topology_obj, provisioning: str) -> ResultsMetadataT:
    '''
    Update the firmware of the given system package.

    Args:
        devices: Devices object containing the devices to update.
        engines: Engines object containing the engine to update.
        sys_pkg: System package containing the recipe and NVOS.
        provisioning: Provisioning to update to.

    Returns:
        dict[Metadata, Any]: Metadata of the FW update.
    '''
    logger.info("Starting Upgrade Action")
    metadata: ResultsMetadataT = {}
    scp_path = 'scp://{}:{}@{}'.format(engines.sonic_mgmt.username, engines.sonic_mgmt.password, IpTool.format_ip_for_uri(engines.sonic_mgmt))

    with allure.step("Get system current FW versions"):
        current_fw_versions = get_platform_fw_versions()

    recipe = _load_recipe(sys_pkg, provisioning)
    single_platform_fw_recipe = _get_single_platform_fw_recipe(recipe) if devices.dut.single_fw_pkg_supported else None

    with allure.step("Update FW"):
        if single_platform_fw_recipe:
            results = _fetch_n_update_single_platform_fw(single_platform_fw_recipe, scp_path)
        else:
            results = _fetch_n_update_fw(devices.dut.constants.firmware, recipe, scp_path, current_fw_versions, devices)
            results.extend(_update_cpld(recipe, scp_path, current_fw_versions, devices))
        logger.debug(f'[***] {results=}')
        if any(res.duration_error for res in results):
            metadata.setdefault(ResultMetadata.DURATION_ERROR, {}).update({
                f'[{sys_pkg.name}] {res.operation}': res.duration_error for res in results if res.duration_error
            })

        assert all(results), "Failed to update components firmware: %s" % " | ".join(map(str, filter(lambda res: not res, results)))

    with allure.step(f"Update NVOS to {sys_pkg.nvos.name!r}"):
        assert (nvos_result := _update_nvos(engines, topology_obj, sys_pkg.nvos)), f"Failed to update NVOS to {sys_pkg.nvos.name}"
        logger.debug(f'[***] {nvos_result=}')
        if nvos_result.duration_error:
            metadata.setdefault(ResultMetadata.DURATION_ERROR, {})[f'[{sys_pkg.name}] nvos install'] = nvos_result.duration_error

    if nvos_result.skipped and any(results):
        with allure.step("Perform power-cycle"):
            System().action_reboot(flags='force').verify_result()

    with allure.step("Get New FW versions"):
        new_fw_versions = get_platform_fw_versions()
        metadata[ResultMetadata.HAD_FW_UPDATE] = _platform_fw_versions_changed(current_fw_versions, new_fw_versions)
        logger.debug(f'[***] {metadata=}')

    with allure.step("Verify FW versions"):
        single_pkg = bool(single_platform_fw_recipe)
        assert (r := _verify_fw_versions(devices.dut.constants.firmware, recipe, new_fw_versions, single_pkg=single_pkg)), r.error_message

    return metadata


def load_config(engines: EnginesT, config_file: Path | str) -> None:
    '''
    Load the configuration file to the DUT.

    Args:
        engines: Engines object containing the engine to update.
        config_file: Path to the configuration file.
    '''
    if not isinstance(config_file, Path):
        config_file = Path(config_file)

    remote_url = ImageConsts.SCP_PATH_SERVER.format(
        username=engines.sonic_mgmt.username,
        password=engines.sonic_mgmt.password,
        ip=IpTool.format_ip_for_uri(engines.sonic_mgmt),
        path=config_file,
    )

    system = System()
    action_expected_str = "File fetched successfully"

    with allure.step("Disable password hardening"):
        system.security.password_hardening.set(SystemConsts.USERNAME_PASSWORD_HARDENING_STATE, SystemConsts.USER_STATE_DISABLED, apply=True).verify_result()

    with allure.step("Fetch and replace config file"):
        system.config.action_fetch(remote_url, base_url="", expected_output=action_expected_str).verify_result()
        output = NvueGeneralCli.replace_config(engines.dut, config_file.name, verify_execution=True)
        assert "Error" not in output, f"Failed to replace config {config_file.name}"

    with allure.step("Set admin password"):
        system.aaa.user.user_id[SystemConsts.DEFAULT_USER_ADMIN].set(AaaConsts.PASSWORD, PwhConsts.ADMIN).verify_result()

    with allure.step("Apply and save config"):
        NvueGeneralCli.apply_config(engine=engines.dut, option='-y')
        NvueGeneralCli.save_config(engine=engines.dut)


def rollback(current_build: str | None = None, other_build: str | None = None, boot_next_partition_id: str | None = None) -> str | None:
    """
    Rollback to the previous image partition.

    Args:
        current_build_id: The current build ID. (e.g. nvos-amd64-25.02.2341.bin)
        boot_next_partition_id: The name of the rollback partition.

    Returns:
        The name of the current partition ID.

    Raises:
        AssertionError: If the previous image partition is not found.
    """
    assert current_build or boot_next_partition_id, f"Either {current_build=} or {boot_next_partition_id=} must be provided"

    system, current_partition_id = System(), None
    if not boot_next_partition_id:
        sys_image = get_system_image()

        assert sys_image.other, "There is no second partition"
        assert sys_image.current == current_build.replace('-amd64', '').replace('.bin', ''), f"Current build ID {sys_image.current} is not the same as the current partition {current_build}"
        assert sys_image.other == other_build.replace('-amd64', '').replace('.bin', ''), f"Other build ID {sys_image.other} is not the same as the current partition {other_build}"

        boot_next_partition_id = sys_image.other_partition
        current_partition_id = sys_image.current_partition

    system.image.boot_next_and_verify(boot_next_partition_id)
    system.action_reboot(flags='force').verify_result()
    return current_partition_id


def _extract_leaf_paths(d, prefix=""):
    """Extract dotted paths to leaf values from a nested dict for readable diff summaries."""
    paths = []
    for key, value in d.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            paths.extend(_extract_leaf_paths(value, path))
        else:
            paths.append(f"{path} = {value}")
    return paths


def compare_configs(engines: EnginesT, config_file: Path) -> None:
    """
    Compare the system config with the expected config.

    Args:
        engines: Engines object containing the engine to update.
        config_file: Path to the configuration file.
    """
    dicts_diff = NvosInstallationSteps.verify_config_after_upgrade(config_file, engines.dut)
    if dicts_diff:
        missing_keys = _extract_leaf_paths(dicts_diff)
        summary = "\n".join(f"  - {path}" for path in missing_keys)
        assert False, (
            f"Configuration was not preserved across upgrade.\n"
            f"Missing/mismatched settings ({len(missing_keys)}):\n{summary}\n\n"
            f"Full diff:\n{yaml.dump(dicts_diff, default_flow_style=False)}"
        )


def remove_added_fw_files(devices: DevicesT, delete_platform_firmware_files: bool = False) -> None:
    """ Remove all existing firmware files. """
    platform = Platform()
    if delete_platform_firmware_files and devices.dut.single_fw_pkg_supported:
        platform.firmware.files.delete_all_existing_files()
        return

    for component_name in map(str.lower, devices.dut.constants.firmware):
        if component_name.startswith(('cpld', 'ssd')):
            continue

        component: PlatformComponent | Asic | None = getattr(platform.firmware, component_name, None)
        if not component:
            continue

        component.files.delete_all_existing_files()


def remove_added_config_files() -> None:
    """ Remove all existing config files. """
    System().config.files.delete_all_existing_files()


def get_system_image() -> SystemImage:
    """
    Get the system image.

    Returns:
        SystemImage: The system image.
    """
    result = System().image.get_image_field_values()
    if result[ImageConsts.CURRENT_IMG] in ('1', '2'):
        partition = 'partition%s' % result["current"]
        other_partition = ImageConsts.PARTITION1_IMG if result[ImageConsts.CURRENT_IMG] == '2' else ImageConsts.PARTITION2_IMG
        return SystemImage(
            current=(result[partition] or {}).get('build-id'),
            current_partition=partition,
            other=(result[other_partition] or {}).get('build-id'),
            other_partition=other_partition,
        )

    current_partition = ImageConsts.PARTITION1_IMG if result[ImageConsts.CURRENT_IMG] == result[ImageConsts.PARTITION1_IMG] else ImageConsts.PARTITION2_IMG
    other_partition = ImageConsts.PARTITION1_IMG if result[ImageConsts.CURRENT_IMG] != result[ImageConsts.PARTITION1_IMG] else ImageConsts.PARTITION2_IMG
    return SystemImage(
        current=result[ImageConsts.CURRENT_IMG],
        current_partition=current_partition,
        other=result[other_partition],
        other_partition=other_partition,
    )


def get_platform_fw_versions(platform: Platform | None = None) -> dict[str, str | None]:
    """ Get the platform firmware versions. """
    platform_fw_versions: dict[str, dict[str, str]] = (platform or Platform()).firmware.parse_show()
    return {k: v.get(PlatformConsts.FW_ACTUAL) for k, v in platform_fw_versions.items()}
