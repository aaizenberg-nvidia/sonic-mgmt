#!/usr/bin/env python

from pathlib import Path
import logging
import allure
import json
import re

from ngts.scripts.code_coverage.code_coverage_consts import SharedConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.helpers import system_helpers

logger = logging.getLogger(__name__)


def _create_and_chmod(path: Path, /, *, mode: int = 0o777) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:  # just in case
        path.chmod(mode)
    except Exception as e:
        logger.warning(f"Could not chmod {path}: {e}")


def _get_nvos_version(engine: system_helpers.PrefixEngine) -> str:
    output: dict[str, str] = json.loads(engine.run_cmd("nv show system version -o json"))
    if not (nvos_version := output.get('version')):
        if not (nvos_version := output.get('image', {}).get('build-id')):
            if not (nvos_version := output.get('product-release')):  # last resort
                raise ValueError(f"Could not extract nvos version from output: {output}")
    logger.debug(f'{nvos_version=!r}')
    return nvos_version


def _get_coverage_dir_name(nvos_version: str) -> str:
    version_name = nvos_version[5:] if nvos_version.startswith("nvos-") else nvos_version

    if version_name.startswith("dev-"):
        match = re.fullmatch(r"(?P<branch>dev-.+)-\d{3}", version_name)
        if not match:
            raise ValueError(f"Could not extract feature branch from NVOS version: {nvos_version}")
        branch_name = match.group("branch")
    else:
        branch_name = TestToolkit.version_to_release(f"nvos-{version_name}")
        if not branch_name:
            raise ValueError(f"Could not extract release branch from NVOS version: {nvos_version}")

    return f"{branch_name}_{version_name}"


def get_dest_path(engine: system_helpers.PrefixEngine, coverage_path: str) -> Path:
    """
    Creates a destination path for coverage reports based on the NVOS version.

    Args:
        engine: The engine object to get system information from
        coverage_path: Base coverage path

    Returns:
        str: The destination path for coverage reports
    """
    with allure.step("Get nvos version"):
        nvos_version = _get_nvos_version(engine)
        coverage_dir_name = _get_coverage_dir_name(nvos_version)
        logger.debug(f'{coverage_dir_name=!r}')

    with allure.step("Create coverage folder if not exists"):
        _create_and_chmod(dest := Path(coverage_path) / coverage_dir_name)
        logger.debug(f'{dest=!r}')
        _create_and_chmod(dest / SharedConsts.C_DIR)
        _create_and_chmod(dest / SharedConsts.PYTHON_DIR)

    return dest


def get_coverage_path_from_target_version(target_version: str) -> str:
    """
    Transforms a target version path into a coverage path.

    Args:
        target_version: Path to the target version

    Returns:
        str: The coverage path

    Example:
        Input: /auto/sw_system_release/nos/nvos/25.02.4934-024/amd64/dev/nvos-amd64-25.02.4934-024.bin
        Output: /auto/sw_system_release/nos/nvos/25.02.4934-024/amd64/dev/coverage
    """
    path_parts = target_version.split('/')
    path_parts.pop()
    coverage_path = '/'.join(path_parts) + '/coverage'
    return coverage_path
