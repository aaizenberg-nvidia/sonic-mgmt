import re
import pytest
import logging
import allure
import sys
from tests.common import config_reload
current_path = sys.path.copy()
sys.path.insert(0, "dash")
from tests.common.helpers.smartswitch_util import correlate_dpu_info_with_dpuhost
from tests.common.helpers.sonic_image_readme import resolve_sonic_image_readme_path
sys.path = current_path

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.skip_check_dut_health
]

logger = logging.getLogger(__name__)

COMPONENT_SCRIPT_NAME = "get_component_versions.py"
README_COVERED_COMPONENTS = ['SDK', 'FW', 'SAI', 'MFT', 'KERNEL', 'BFSOC']

# Commands to directly fetch component versions from DPU
COMMANDS_FOR_ACTUAL = {
    "MFT": {"cmd": "dpkg -l | grep 'mft '", "regex": "mft *([0-9.-]*)"},
    "SDK": {"cmd": "docker exec syncd bash -c 'dpkg -l | grep sdn'", "regex": "sdn-appliance *([0-9.-]*mlnx1)"},
    "SAI": {"cmd": "docker exec syncd bash -c 'dpkg -l | grep mlnx-sai'", "regex": ".*1\\.mlnx\\.([A-Za-z0-9.]*)"},
    "FW": {"cmd": "mlxfwmanager --query | grep -e 'FW *[0-9.]*'", "regex": "FW * [0-9]{2}\\.([0-9.]*)"},
    "KERNEL": {"cmd": "uname -r", "regex": "(.*)-[a-z0-9]+$"},
    "BFSOC": {"cmd": "dpkg -l | grep mlxbf-bootimages", "regex": "mlxbf-bootimages *([0-9.-]*)"}
}

# These values appear when a component doesn't have a compilation or actual version
NON_EXISTENT_VERSION = '-'
UNAVAILABLE_VERSION = 'N/A'


def parse_component_version_table(dpuhost):
    """
    Parse the component version table from the output of get_components_version.py script on DPU

    :param dpuhost: dpuhost fixture
    :return: Dictionary with component names as keys and tuples of (compilation_version, actual_version) as values
    """
    parsed_table = dpuhost.show_and_parse(f"sudo {COMPONENT_SCRIPT_NAME}")

    version_dict = dict()
    for component_info in parsed_table:
        component = component_info['component']
        # We cannot guarantee that SimX version will be aligned
        if component == 'simx':
            continue
        compilation_version = component_info['compilation']
        actual_version = component_info['actual']
        version_dict[component.upper()] = (compilation_version, actual_version)
    logger.info(f"Parsed components from {COMPONENT_SCRIPT_NAME} are (compilation, actual): {version_dict}")
    return version_dict


def parse_readme_versions(sonic_image):
    """
    Parse the component versions from the README file of the sonic image

    :param sonic_image: The current sonic image deployed on the DPU
    :return: Dictionary with component names as keys and version strings as values
    """
    readme_path = resolve_sonic_image_readme_path(sonic_image)
    if not readme_path:
        raise Exception(
            f"Sonic image path: /auto/sw_system_release/sonic/{sonic_image}/dev/README "
            f"doesn't include a README file"
        )

    logger.info(f"Parsing versions according to readme file: {readme_path}")
    with open(readme_path) as f:
        image_readme_content = f.read()

    readme_versions_dict = {}

    # Patterns to match component versions in the README
    # First try to match DPU-specific versions, then fall back to generic versions
    patterns = [
        re.compile(r"(?P<component>\w+)_VERSION_DPU:\s*(?P<version>[^(]+)(?:\s*\(Overrides.*\))?"),
        re.compile(r"(?P<component>\w+)_VERSION:\s*(?P<version>[^(]+)(?:\s*\(Overrides.*\))?")
    ]

    for line in image_readme_content.strip().split('\n'):
        for pattern in patterns:
            match = pattern.match(line)
            if match:
                component = str(match.group('component').strip())
                # Add only if component is in README_COVERED_COMPONENTS and not already stored
                if component in README_COVERED_COMPONENTS and component not in readme_versions_dict:
                    # Extract the version and remove any trailing whitespace
                    version = str(match.group('version').strip())
                    readme_versions_dict[component] = version
                break

    logger.info(f"Parsed components from {readme_path} are:\n {readme_versions_dict}")
    return readme_versions_dict


def get_actual_version(dpuhost, component):
    """
    Fetch the current version of a component from the DPU by running the appropriate command

    :param dpuhost: The DPU host
    :param component: The component to fetch version for
    :return: The version of the component as it appears on the DPU
    """

    cmd = COMMANDS_FOR_ACTUAL[component]["cmd"]
    version_output = dpuhost.shell(cmd)["stdout"]

    regex_pattern = COMMANDS_FOR_ACTUAL[component]["regex"]
    parsed_version = re.search(regex_pattern, version_output)

    return parsed_version.group(1) if parsed_version else UNAVAILABLE_VERSION


def fetch_versions_from_dpu(dpuhost):
    """
    Fetch versions of all components installed on the DPU in runtime

    :param dpuhost: The DPU host
    :return: Dictionary with component names as keys and version strings as values
    """
    actual_versions_dict = {}
    for component in COMMANDS_FOR_ACTUAL:
        actual_versions_dict[component] = get_actual_version(dpuhost, component)

    logger.info(f"Components fetched from the DPU are {actual_versions_dict}")
    return actual_versions_dict


@pytest.fixture(scope="module")
def readme_versions(duthost):
    """
    Fixture to fetch the versions of components listed in the README file of the image running on the DPU

    :param duthost: duthost fixture
    :return: Dictionary with component names as keys and version strings as values
    """
    # Get the sonic image version from the DUT
    sonic_image = duthost.os_version
    logger.info(f"Sonic image version: {sonic_image}")
    sonic_image = sonic_image.replace("_ASAN", "")

    yield parse_readme_versions(sonic_image)


@pytest.fixture(scope="module")
def dpu_component_table(dpuhosts):
    """
    Fixture to get the component version table from the DPU

    :param dpuhosts: dpuhosts fixture
    :return: List of dictionaries with component versions parsed from the table
    """
    return [parse_component_version_table(dpuhost) for dpuhost in dpuhosts]


@allure.title('Test DPU Component Versions')
def test_dpu_component_versions(readme_versions, dpu_component_table, dpuhosts):
    """
    Verify that the component versions on the DPU match what's expected

    This test:
    1. Compares compilation versions from the component table with README versions
    2. Compares actual versions from the component table with directly fetched versions

    :param readme_versions: readme_versions fixture
    :param dpu_component_table: dpu_component_table fixture
    :param dpuhosts: dpuhosts fixture
    """
    for dpu_index, dpu_host in enumerate(dpuhosts):
        logger.info(f"Testing DPU index {dpu_index}, host: {dpu_host}")
        actual_versions = fetch_versions_from_dpu(dpu_host)

        # Check if all components from README are in the component table
        for component in README_COVERED_COMPONENTS:
            if component in readme_versions and component in dpu_component_table[dpu_index]:
                # Extract the compilation and actual versions from the component table
                compilation_version, table_actual_version = dpu_component_table[dpu_index][component]

                # Test 1: Check if compilation version matches the README version
                readme_version = readme_versions[component]
                if component == 'BFSOC':
                    readme_version = readme_version.split(':')[-1]
                assert compilation_version == readme_version, \
                    f"Compilation version for {component} in component table ({compilation_version}) " \
                    f"doesn't match README version ({readme_version})"

                # Test 2: Check if actual version in the table matches the directly fetched version
                direct_actual_version = actual_versions[component]
                assert table_actual_version == direct_actual_version, \
                    f"Actual version for {component} in component table ({table_actual_version}) " \
                    f"doesn't match directly fetched version ({direct_actual_version})"
