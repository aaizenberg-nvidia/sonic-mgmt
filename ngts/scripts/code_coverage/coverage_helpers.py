#!/usr/bin/env python

from pathlib import Path
import logging
import allure
import json
import os

from ngts.scripts.code_coverage.code_coverage_consts import SharedConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.helpers import system_helpers

logger = logging.getLogger(__name__)

FALLBACK_COVERAGE_BASE = Path("/auto/sw_system_project/NVOS_INFRA/coverage/fallback_coverage")


def _create_and_chmod(path: Path, /, *, mode: int = 0o777) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:  # just in case
        path.chmod(mode)
    except Exception as e:
        logger.warning(f"Could not chmod {path}: {e}")


def _is_writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


def _get_nvos_version(engine: system_helpers.PrefixEngine) -> str:
    output: dict[str, str] = json.loads(engine.run_cmd("nv show system version -o json"))
    if not (nvos_version := output.get('version')):
        if not (nvos_version := output.get('image', {}).get('build-id')):
            if not (nvos_version := output.get('product-release')):  # last resort
                raise ValueError(f"Could not extract nvos version from output: {output}")
    logger.debug(f'{nvos_version=!r}')
    return nvos_version


def get_dest_path(engine: system_helpers.PrefixEngine, coverage_path: str) -> Path:
    """
    Creates a destination path for coverage reports based on the NVOS version.
    If the primary path cannot be written to (e.g. owned by a different user),
    falls back to FALLBACK_COVERAGE_BASE which is writable by all runs.

    Args:
        engine: The engine object to get system information from
        coverage_path: Base coverage path

    Returns:
        Path: The destination path for coverage reports
    """
    with allure.step("Get nvos version"):
        nvos_version = _get_nvos_version(engine)
        release: str = TestToolkit.version_to_release(nvos_version)
        logger.debug(f'{release=!r}')
        nvos_version = nvos_version.replace("nvos-", "")
        logger.debug(f'{nvos_version=!r}')

    version_dir = f"{release}_{nvos_version}"

    with allure.step("Create coverage folder if not exists"):
        primary_dest = Path(f"{coverage_path}/{version_dir}")
        try:
            _create_and_chmod(primary_dest)
            if not _is_writable(primary_dest):
                raise OSError(f"Directory {primary_dest} is not writable")
            dest = primary_dest
        except Exception as e:
            dest = FALLBACK_COVERAGE_BASE / version_dir
            logger.warning(
                f"Could not use primary coverage path {primary_dest}: {e}. "
                f"Falling back to {dest}"
            )
            _create_and_chmod(dest)

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
