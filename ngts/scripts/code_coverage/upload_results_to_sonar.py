from allure_commons.types import AttachmentType
from pathlib import Path
import logging
import shutil
import pytest
import json
import enum
import re

from ngts.nvos_tools.infra.JenkinsTool import JenkinsQueryBuilder, JenkinsTool
from ngts.nvos_tools.Devices.DeviceFactory import DeviceFactory
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tools.test_utils import allure_utils as allure
from ngts.scripts.code_coverage import coverage_helpers
from ngts.ngts_types import EnginesT, TopologyT

from .code_coverage_consts import NvosConsts, SharedConsts

logger = logging.getLogger(__name__)

_CONFIG_FILES_DIR = (Path(__file__).parent / 'config_files').resolve().absolute()
_PYTHON_JOB_NAME = "NVOS_Python_upload_results_to_sonar"
_CPP_JOB_NAME = "NVOS_C_upload_results_to_sonar"


class ConfigLanguage(enum.IntEnum):
    PYTHON = enum.auto()
    CPP = enum.auto()


class ConfigFileType(enum.IntEnum):
    INCLUDE = enum.auto()
    EXCLUDE = enum.auto()


_CONFIG_FILES_NAMES: dict[ConfigLanguage, dict[ConfigFileType, str]] = {
    ConfigLanguage.PYTHON: {
        ConfigFileType.INCLUDE: "nvos_coverage_python_include.conf",
        ConfigFileType.EXCLUDE: "nvos_coverage_python_exclude.conf",
    },
    ConfigLanguage.CPP: {
        ConfigFileType.INCLUDE: "nvos_coverage_cpp_include.conf",
        ConfigFileType.EXCLUDE: "nvos_coverage_cpp_exclude.conf",
    },
}


@pytest.fixture(scope='module')
def results_path(topology_obj: TopologyT, engines: EnginesT) -> Path:
    with allure.step("Create device object if needed"):
        if not TestToolkit.devices:
            devices = DeviceFactory.create_devices_object(topology_obj)
            TestToolkit.update_devices(devices)

    with allure.step("Create coverage report path"):
        return coverage_helpers.get_dest_path(engines.dut, NvosConsts.DEST_PATH)


@pytest.mark.disable_loganalyzer
@allure.orig_allure.title('Upload results to sonar')
def test_upload_results_to_sonar(target_version: str, results_path: Path, branch_n_commit_id: tuple[str, str] | None) -> None:
    try:
        with allure.step("Get coverage results paths, branch name and commit id"):
            python_results_path = results_path / SharedConsts.PYTHON_DIR
            logger.info(f"python results path: {python_results_path}")

            cpp_results_path = results_path / SharedConsts.C_DIR
            logger.info(f"cpp results path: {cpp_results_path}")

            branch_name, commit_id = (branch_n_commit_id or _get_branch_and_commit_id(results_path))
            logger.info(f"branch name: {branch_name}")
            logger.info(f"commit id: {commit_id}")

        client = JenkinsTool(project_job_path=SharedConsts.JENKINS_SONAR_PROJECT_PATH)
        general_job_params = (
            JenkinsQueryBuilder()
            .branch(branch_name)
            .commit_id(commit_id)
            .version(1)
            .mailing_list(["yport,ramih"])
        )

        sonar_upload_jobs: tuple[tuple[ConfigLanguage, str, str, Path], ...] = (
            (ConfigLanguage.PYTHON, "NVOS-Python", _PYTHON_JOB_NAME, python_results_path),
            (ConfigLanguage.CPP, "NVOS_CPP", _CPP_JOB_NAME, cpp_results_path),
        )

        config_files_names = _CONFIG_FILES_NAMES.copy()
        mapper: dict[str, tuple[Path, Path]] = {}
        with allure.step("Validate config files"):
            for lang, items in config_files_names.items():
                lang_title = lang.name.upper() if lang == ConfigLanguage.CPP else lang.name.title()
                for file_type, conf_file_name in items.items():
                    src = _CONFIG_FILES_DIR / conf_file_name
                    with allure.independent_step(f"Validate {lang_title} {file_type.name.title()} config file {src.name!r}"):
                        if not src.exists():
                            raise FileNotFoundError(f"Config file {str(src)!r} not found")

                    dst = Path(NvosConsts.DEST_PATH) / f'{file_type.name.lower()}_lists' / f'{branch_name}_{conf_file_name}'
                    mapper[conf_file_name] = (src, dst)

        for lang, proj, job_name, result_path in sonar_upload_jobs:
            lang_title = lang.name.upper() if lang == ConfigLanguage.CPP else lang.name.title()
            with allure.step(f"Copy {lang_title} coverage conf files to NFS with branch prefix {branch_name!r}"):
                for file_type, conf_file_name in config_files_names[lang].items():
                    src, dst = mapper[conf_file_name]
                    dst.parent.mkdir(parents=True, exist_ok=True)

                    logger.info(f"Copying {src} to {dst}")
                    shutil.copy2(src, dst)
                    config_files_names[lang][file_type] = str(dst)
                attachment_name = f"{lang_title.lower()}-config-files.json"
                allure.attach(attachment_name, json.dumps(config_files_names[lang], indent=2), AttachmentType.JSON)

            with allure.step(f'Upload {lang_title} coverage results to sonar'):
                job_params = (
                    general_job_params
                    .project(proj)
                    .coverage_folder(result_path)
                    .include_file_path(config_files_names[lang][ConfigFileType.INCLUDE])
                    .exclude_file_path(config_files_names[lang][ConfigFileType.EXCLUDE])
                    .build()
                )
                logger.info(f"Triggering {job_name} with params: {job_params}")
                client.trigger_with_query(job_name, job_params)

    except Exception as err:
        raise AssertionError(err)


@pytest.mark.disable_loganalyzer
def test_copy_unitests_results(engines: EnginesT, target_version: str, topology_obj: TopologyT, results_path: Path) -> None:
    """
    Copies unitests coverage XML files from the coverage directory to the destination path.
    The function finds all XML files in the coverage directory and its sub-directories
    and copies them to the destination path.

    Args:
        engines: Test engines object containing DUT information
        target_version: Path to the target version, used to determine the coverage path
        topology_obj: Topology object containing DUT information
    """
    with allure.step("Copy unitests results"):
        with allure.step("Get coverage path from target version"):
            coverage_path = coverage_helpers.get_coverage_path_from_target_version(target_version)
            logger.info(f"coverage path: {coverage_path}")

        with allure.step("Check if destination path exists"):
            with allure.step("Create device object if needed"):
                devices = DeviceFactory.create_devices_object(topology_obj)
                TestToolkit.update_devices(devices)
            dest = results_path / SharedConsts.PYTHON_DIR
            logger.info(f"destination path: {dest}")

        with allure.step("Copy unitests results"):
            for file in map(Path.resolve, Path(coverage_path).rglob('*.xml')):
                dir_path = str(file.parent.relative_to(coverage_path)).replace('/', '_')
                new_filename = f"{dir_path}-{file.name}" if dir_path else file.name
                dest_file = dest / new_filename

                dest_file.unlink(True)  # delete file if it exists
                shutil.copy2(str(file), str(dest_file))

            logger.info(f"Unitests results: {dest}")


def _get_branch_and_commit_id(results_path: Path) -> tuple[str, str]:
    """
    Extract the Sonar branch and git commit ID from a coverage results path.

    Args:
        results_path: Coverage directory named using the branch and build version.

    Returns:
        tuple containing the prefixed Sonar branch and unmodified git commit ID.

    Examples:
        'nvos-25-03-0800_25.03.0720' -> ('rel-nvos-25-03-0800', 'nvos-25-03-0800_25.03.0720')
        'dev-system-alarms_dev-system-alarms-005' -> ('fb-dev-system-alarms', 'dev-system-alarms_dev-system-alarms-005')
    """
    commit_id = results_path.name
    release_match = re.fullmatch(
        r"(?P<branch>nvos-(?P<major>\d{2})-(?P<minor>\d{2})-\d{4})_"
        r"(?P=major)\.(?P=minor)\.\d{4}(?:-\d{3})?",
        commit_id,
    )
    if release_match:
        return f"rel-{release_match.group('branch')}", commit_id

    feature_match = re.fullmatch(r"(?P<branch>dev-.+)_(?P=branch)-\d{3}", commit_id)
    if feature_match:
        return f"fb-{feature_match.group('branch')}", commit_id

    raise ValueError(f"Could not extract branch name and commit ID from coverage path: {results_path}")
