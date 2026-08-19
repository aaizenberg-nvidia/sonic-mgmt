from typing import Generator, Optional, Dict, Any, List
from functools import partial
import logging
import pytest

from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.ngts_types import EnginesT, DevicesT
from ngts.tests_nvos.constants import MINUTE

from . import helpers, feature_checkers, post_checkers

logger = logging.getLogger(__name__)


def _test_id_builder(case: helpers.SystemVersionTransition):
    return f"{case.action.name.lower()}][%s" % "-".join(
        p.nvos.name.replace(p.nvos.suffix, '').replace('nvos-', '').replace('amd64-', '')
        for p in (case.base, case.target) if p
    )


@pytest.fixture(scope='module', autouse=True)
def ensure_target_version_is_installed(target_version: str):
    """ One extra step to ensure the target version is installed """
    yield

    if not target_version:
        return

    with allure.step("uninstall previous version"):
        System().image.action_uninstall(params="force", verify_res=False)


def pytest_generate_tests(metafunc: pytest.Metafunc):
    """ Generate test cases from the matrix file/json. """
    matrix: List[Dict[str, Any]] = metafunc.config.getoption("--upgrade-matrix-json", skip=True)  # type: ignore
    target_version: str = metafunc.config.getoption("--target_version", None)  # type: ignore
    override_target_version: bool = metafunc.config.getoption("--override-target-version", False)  # type: ignore

    _matrix: List[helpers.SystemVersionTransition] = []
    for item in matrix:
        if override_target_version:
            item['target']['nvos'] = target_version

        prev_case: Optional[helpers.SystemVersionTransition] = None
        for action in item['actions']:
            _matrix.append(case := helpers.SystemVersionTransition.from_dict({
                "base": item['base'],
                "target": item['target'],
                "action": action,
            }))

            if case.action == helpers.Action.UPGRADE_NO_CONFIG_FILE:
                if prev_case and prev_case.action == helpers.Action.ROLLBACK:
                    prev_case.skip_rollback_cleanup = True

            prev_case = case
    metafunc.parametrize("case", _matrix, ids=_test_id_builder)


def _rollback(base: helpers.SystemPackage, target: helpers.SystemPackage, register_cleanup, skip_rollback_cleanup: bool = False,
              **kwargs) -> helpers.ResultsMetadataT:
    """ Rollback the system to the base version.

    Args:
        base: The base system package.
        target: The target system package.
        register_cleanup: The register cleanup function.
        skip_rollback_cleanup: Whether to skip the rollback cleanup.
        **kwargs: Additional keyword arguments.
    """
    base_platform_fw_versions = helpers.get_platform_fw_versions()
    result_metadata: helpers.ResultsMetadataT = {}
    with allure.step("Rollback system"):
        current_partition = helpers.rollback(base.nvos.name, other_build=target.nvos.name)

    target_platform_fw_versions = helpers.get_platform_fw_versions()
    if base_platform_fw_versions != target_platform_fw_versions:
        diff = {
            k: f"base={v1!r} != target={v2!r}"
            for k in base_platform_fw_versions.keys() | target_platform_fw_versions.keys()
            if (v1 := base_platform_fw_versions.get(k)) != (v2 := target_platform_fw_versions.get(k))
        }
        logger.info(f"FW versions changed from: {diff}")
        result_metadata[helpers.ResultMetadata.HAD_FW_UPDATE] = True

    if skip_rollback_cleanup:
        logger.info("Skipping rollback cleanup")
    else:
        # the test passed, so we need to rollback to the target version after the test is finished
        logger.info("Registering rollback cleanup")
        register_cleanup(partial(helpers.rollback, boot_next_partition_id=current_partition))

    return result_metadata


def _upgrade_downgrade(*, base: helpers.SystemPackage, target: helpers.SystemPackage, engines: EnginesT, devices: DevicesT,
                       topology_obj, provisioning: str, feature_checkers_it: Optional[Generator[List[helpers.Result], None, List[helpers.Result]]],
                       register_cleanup, load_config: bool = True, is_upgrade: bool = True, **kwargs) -> helpers.ResultsMetadataT:
    """
    Update the system to the base version and return True if any firmware was updated successfully.

    Args:
        base: The base system package.
        target: The target system package.
        engines: The engines object.
        devices: The devices object.
        topology_obj: The topology object.
        provisioning: The provisioning.
        feature_checkers_it: The feature checkers generator.
        register_cleanup: The register cleanup function.
        load_config: Whether to load the configuration.

    Returns:
        bool: True if any firmware was updated, False otherwise.
    """
    result_metadata: helpers.ResultsMetadataT = {}

    with allure.step("uninstall previous version"):
        System().image.action_uninstall(params="force", verify_res=False)

    with allure.step(f"Update system to {base.nvos.name!r}"):
        register_cleanup(partial(helpers.remove_added_fw_files, devices, delete_platform_firmware_files=True))
        helpers.update_system_fw(devices, engines, base, topology_obj, provisioning)

    if load_config:  # case of downgrade we don't load config
        with allure.step("Load Configuration"):
            register_cleanup(helpers.remove_added_config_files)
            helpers.load_config(engines, base.config)

    if is_upgrade and feature_checkers_it:
        with allure.step("Run feature pre-checkers"):
            if feature_checkers_errors := next(feature_checkers_it):
                errors_str = "Pre-checkers failed:\n\t%s" % "\n\t".join(map(str, feature_checkers_errors))
                logger.error(errors_str)
                result_metadata[helpers.ResultMetadata.FEATURE_CHECKER_ERROR] = errors_str

    with allure.step("uninstall previous version"):
        System().image.action_uninstall(params="force", verify_res=False)

    with allure.step(f"Update system to {target.nvos.name!r}"):
        result_metadata.update(helpers.update_system_fw(devices, engines, target, topology_obj, provisioning))

    return result_metadata


def _check_if_base_version_is_expected_version(base: helpers.SystemPackage) -> None:
    with allure.step("Verify system version is the expected version"):
        system_image = helpers.get_system_image()
        # the current version is the base version for upgrade and rollback, as for downgrade it is the target version
        expected_version = base.nvos.name.replace('amd64-', '').replace('.bin', '')
        assert expected_version == system_image.current, f"Current image is {system_image.current} but expected {expected_version}"


def _build_upgrade_downgrade_rollback_kwargs(case: helpers.SystemVersionTransition, engines: EnginesT, devices: DevicesT,
                                             topology_obj, provisioning: str, register_cleanup, unregister_cleanup) -> Dict[str, Any]:
    """ Build the kwargs for the upgrade/downgrade/rollback function. """
    kwargs = dict(
        base=case.base,
        target=case.target,
        engines=engines,
        devices=devices,
        topology_obj=topology_obj,
        provisioning=provisioning,
        register_cleanup=register_cleanup,
        unregister_cleanup=unregister_cleanup,
        expected_version=case.base.nvos.name,
        feature_checkers_it=None,
        is_upgrade=case.action.is_upgrade,
        load_config=case.action == helpers.Action.UPGRADE,
    )

    if case.action == helpers.Action.ROLLBACK:
        kwargs.update(
            base=case.target,
            target=case.base,
            skip_rollback_cleanup=case.skip_rollback_cleanup,
        )

    if case.action == helpers.Action.DOWNGRADE:
        kwargs.update(
            target=case.base,
            base=case.target,
            expected_version=case.target.nvos.name,
        )

    logger.debug(f"kwargs: {kwargs}")
    return kwargs


@pytest.mark.timeout(MINUTE * 90, func_only=True)
def test_change_nvos_version(case: helpers.SystemVersionTransition, engines: EnginesT, devices: DevicesT, topology_obj,
                             provisioning: str, register_cleanup, unregister_cleanup):
    """
    Test upgrade/downgrade/rollback of NVOS version.

    Steps:
    1. Verify system version is the expected version
    2. Run Action (upgrade/downgrade/rollback)
    3. Verify system config is preserved
    4. Run general checkers
    5. Run feature post-checkers
    6. Verify no errors in steps 4-5
    """
    result_metadata: helpers.ResultsMetadataT = {}
    kwargs = _build_upgrade_downgrade_rollback_kwargs(case, engines, devices, topology_obj, provisioning, register_cleanup, unregister_cleanup)

    # Step 1: Verify system version is the expected version, raise assertion error if not
    _check_if_base_version_is_expected_version(kwargs['base'])

    feature_checkers_it = None
    if case.action == helpers.Action.UPGRADE_NO_CONFIG_FILE:
        # Create a generator for the feature checkers
        kwargs['feature_checkers_it'] = feature_checkers_it = feature_checkers.run_checkers(
            engines=engines,
            devices=devices,
            base=case.base,
            target=case.target,
        )

    # Step 2: Run Action (upgrade/downgrade/rollback)
    if case.action == helpers.Action.ROLLBACK:
        result_metadata.update(_rollback(**kwargs))
    else:
        result_metadata.update(_upgrade_downgrade(**kwargs))

    # Step 3: Verify config is preserved
    if case.action in (helpers.Action.UPGRADE, helpers.Action.ROLLBACK):
        with allure.step("validate config preserved"):
            expected_config = case.target.config if (case.target and case.action == helpers.Action.UPGRADE) else case.base.config
            logger.debug(f'expected_config file: {expected_config}')
            helpers.compare_configs(engines, expected_config)

    # Step 4: Run general checkers
    with allure.step("Validate configuration"):
        errors = post_checkers.run_checkers(
            action=case.action,
            result_metadata=result_metadata,
            engines=engines,
            devices=devices,
            case=case,
        )

    # Step 5: Run feature post-checkers
    if case.action == helpers.Action.UPGRADE_NO_CONFIG_FILE and feature_checkers_it:
        with allure.step("Run feature post-checkers"):
            errors.extend(next(feature_checkers_it))

    # Step 6: Verify no errors in steps 4-6
    with allure.step("check for errors"):
        str_errors = []
        if result_metadata.get(helpers.ResultMetadata.DURATION_ERROR):
            logger.error(err := "* Duration errors:\n\t%s" % "\n\t".join(
                ("%s: %s" % item for item in result_metadata[helpers.ResultMetadata.DURATION_ERROR].items())
            ))
            str_errors.append(err)
        if result_metadata.get(helpers.ResultMetadata.FEATURE_CHECKER_ERROR):
            logger.error(
                err := "* Feature checkers errors:\n\t%s" %
                f'{helpers.ResultMetadata.FEATURE_CHECKER_ERROR} : {result_metadata[helpers.ResultMetadata.FEATURE_CHECKER_ERROR]}'
            )
            str_errors.append(err)
        if errors:
            logger.error(err := "* Checkers failed:\n\t%s" % "\n\t".join(map(str, errors)))
            str_errors.append(err)

        assert not str_errors, "Found Errors:\n%s" % "\n\t".join(str_errors)
