import json
import random
import re

import pytest

from ngts.nvos_constants.constants_nvos import ApiType, OutputFormat, ActionConsts, PeerPortConsts, SystemConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.helpers import redmine_helpers
from ngts.ngts_types import DevicesT, EnginesT
from ngts.tests_nvos.constants import MINUTE


def load_nmx_versions_from_json(devices: DevicesT) -> dict[str, dict[str, str]]:
    """
    Load NMX versions from the JSON file specified in the device configuration.

    Returns:
        dict: Dictionary containing burn_path and burn_version_names
    """
    try:
        with open(devices.dut.nmx_cluster_apps_versions_file_path, 'r') as f:
            versions_data = json.load(f)
        return versions_data['nmx_cluster_apps_versions']
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        raise Exception(f"Failed to load NMX versions from JSON file: {devices.dut.nmx_cluster_apps_versions_file_path}. Error: {e}")


@pytest.fixture(scope='session', autouse=True)
def clear_cluster_package_files():
    yield
    fae = Fae(None)
    nmx_package = fae.cluster.package
    with allure.step('delete fetched nmx package files'):
        files = nmx_package.files.get_files()
        nmx_package.files.delete_files(files_to_delete=files).verify_result()


@pytest.fixture(scope='session', autouse=True)
def enable_cluster_and_stop_apps(setup_name, devices):
    cluster = Cluster()
    ClusterTools.start_cluster(cluster, setup_name, OutputFormat.json, devices=devices)
    for app in devices.dut.expected_cluster_apps:
        cluster.apps.app_name[app].action_stop_cluster_app().verify_result()


@pytest.fixture(scope='function')
def restart_nmx_t_lite():
    """
    Uninstalling nmx-telemetry also removes NMX-T Lite, which is shipped inside that package.
    NVOS never restarts the container itself, so without this it keeps getting errors on its sockets.
    """
    yield
    fae = Fae()
    with allure.step('restart nmx-t-lite by toggling fae peer-port state'):
        fae.system.peer_port.set(op_param_name=PeerPortConsts.STATE,
                                 op_param_value=PeerPortConsts.STATE_DISABLED, apply=True).verify_result()
        fae.system.peer_port.set(op_param_name=PeerPortConsts.STATE,
                                 op_param_value=PeerPortConsts.STATE_ENABLED, apply=True).verify_result()


@pytest.fixture()
def install_apps_if_needed(devices):
    cluster = Cluster()
    fae = Fae()
    output = cluster.apps.show()
    flag = True
    if not output:
        # Load versions from JSON file
        versions_data = load_nmx_versions_from_json(devices)
        for app in devices.dut.expected_cluster_apps:
            default_path = versions_data['burn_path'][app]
            default_version = versions_data['burn_version_names'][app]
            filename = fetch_and_verify_package(fae, app, default_path)
            uninstall_install_and_verify_package(fae, app, filename, default_version)
            flag = False

    return flag


@pytest.mark.fae
@pytest.mark.nmx
@pytest.mark.timeout(8 * MINUTE, func_only=True)
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_nmx_package_good_flow(devices, engines, test_api, install_apps_if_needed, restart_nmx_t_lite):
    """
    Test the good flow of NMX package management.

    Steps:
    1. Set the tested API.
    2. Initialize the FAE and cluster objects.
    3. Retrieve the initial expected apps.
    4. For each app:
        a. Retrieve the default and new version names and paths.
        b. Verify the default version of the app.
        c. Fetch and verify the package.
        d. Uninstall the current package and install the new package.
    5. Verify the start and stop operations of the application.
    6. Delete the package file.
    7. Cleanup:
       a. Revert all applications to their default versions.
    """
    TestToolkit.tested_api = test_api
    apps = devices.dut.expected_cluster_apps

    try:
        if install_apps_if_needed:
            # Load versions from JSON file
            versions_data = load_nmx_versions_from_json(devices)
            for app in apps:
                new_version = versions_data['burn_version_names'][app]
                new_path = versions_data['burn_path'][app]

                nmx_package_flow(app, new_path, new_version)

    finally:
        with allure.step(f'cleanup - returning to default versions'):
            fae = Fae()
            cluster = Cluster()
            engines.dut.run_cmd(f'sudo cp {ClusterConsts.INITIAL_APPS_PATH}* {ClusterConsts.INFRA_PACKAGES_PATH}')
            for app in apps:
                filename, default_version = get_data_from_path(engines, ClusterConsts.INITIAL_APPS_PATH, app)
                uninstall_install_and_verify_package(fae, app, filename, default_version, cluster)
                verify_start_stop(cluster, app)
                delete_package_file(fae, filename)


def get_data_from_path(engines, path, app):
    with allure.step(f'Get default version for {app}'):
        pattern = r'_(\d+\.\d+\.\d+)'
        if app == ClusterConsts.NMX_CONTROLLER:
            prefix = ClusterConsts.NMX_CONTROLLER_PREFIX
        elif app == ClusterConsts.NMX_TELEMETRY:
            prefix = ClusterConsts.NMX_TELEMETRY_PREFIX
        else:
            raise Exception(f'{app} needs to be configured')

        file = engines.dut.run_cmd(f"ls {path} | grep {prefix}")
        # Search for the version pattern in the filename
        match = re.search(pattern, file)

        return file, match.group(1)


def fetch_and_verify_package(fae, app, path):
    with allure.step(f'try to fetch nmx cluster package of {app}'):
        fae.cluster.package.action_fetch(path=path).verify_result()

    with allure.step("Validate file was fetched"):
        filename = path.split('/')[-1]
        fae.cluster.package.files.verify_show_files_output([filename])

    return filename


def uninstall_install_and_verify_package(fae, app, filename, expected_version, cluster):
    with allure.step(f'try to uninstall nmx package app {app}'):
        fae.cluster.apps.app_name[app].action_uninstall()

    with allure.step(f'verify nmx package app {app} not in installed apps'):
        output = OutputParsingTool.parse_show_output_to_dict(cluster.apps.show()).get_returned_value()
        ValidationTool.verify_field_value_exist_in_output_dict(output, app).verify_result(False)

    with allure.step(f'try to install nmx package file {filename}'):
        bug_4899223_active = redmine_helpers.is_bug_active(4899223)
        # For NVUE CLI, check for "Action succeeded" until bug is fixed
        expected_output = "Action succeeded" if bug_4899223_active else SystemConsts.ACTION_INSTALL_SUCCESS_MESSAGES
        install_result = fae.cluster.package.files.file_name[filename].action_install(
            reboot_params=False, force=False, expected_output=expected_output
        )
        if bug_4899223_active and TestToolkit.tested_api == ApiType.OPENAPI:
            # OpenAPI can return state=action_success with empty status; skip string check until bug is fixed
            install_result.ignore_result()
        else:
            install_result.verify_result()

    with allure.step(f'verify installation nmx package file {filename}'):
        ClusterTools.verify_app_version(fae.cluster, app, expected_version)


def verify_start_stop(cluster, app):
    with allure.step(f'try to start stop {app}'):
        cluster.apps.app_name[app].action_start_cluster_app().verify_result()
        nmx_c_expected_state = 'up' if app == ClusterConsts.NMX_CONTROLLER else ''
        ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state=nmx_c_expected_state)
        cluster.apps.app_name[app].action_stop_cluster_app().verify_result()
        nmx_c_expected_state = 'down' if app == ClusterConsts.NMX_CONTROLLER else ''
        ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state=nmx_c_expected_state)


def delete_package_file(fae, filename):
    with allure.step(f'try to delete fetched file {filename}'):
        fae.cluster.package.files.file_name[filename].action_delete().verify_result()


def nmx_package_flow(app, path, new_version):
    """
    Handle the package flow for a given application.

    Steps:
    1. Fetch and verify the package.
    2. Uninstall the current package and install the new package.
    3. Verify the start and stop operations of the application.
    4. Delete the package file.
    """
    fae = Fae()
    cluster = Cluster()
    filename = fetch_and_verify_package(fae, app, path)
    uninstall_install_and_verify_package(fae, app, filename, new_version, cluster)
    verify_start_stop(cluster, app)
    delete_package_file(fae, filename)


@pytest.mark.fae
@pytest.mark.nmx
@pytest.mark.simx
@pytest.mark.timeout(2 * MINUTE, func_only=True)
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_nmx_package_bad_flow(devices, engines, test_name, test_api):
    """
    Test the bad flow of NMX package management.

    Steps:
    1. Set the tested API.
    2. Initialize the FAE and NMX package objects.
    3. Randomly select an application to test.
    4. Attempt to uninstall a non-existing application (expect failure).
    5. Fetch the package for the selected application.
    6. Attempt to install the package without uninstalling the previous version (expect failure).
    7. Verify that the old version is still installed.
    8. Delete the fetched package file and verify its deletion.
    9. Attempt to delete the already deleted file (expect failure).
    """
    TestToolkit.tested_api = test_api
    fae = Fae(None)
    nmx_package = fae.cluster.package
    app_to_test = random.choice(devices.dut.expected_cluster_apps)
    _, default_version = get_data_from_path(engines, ClusterConsts.INITIAL_APPS_PATH, app_to_test)

    # Load versions from JSON file
    versions_data = load_nmx_versions_from_json(devices)
    new_path = versions_data['burn_path'][app_to_test]
    filename = new_path.split('/')[-1]
    non_exist_app = RandomizationTool.get_random_string(8)

    with allure.step(f'try to uninstall non existing app {non_exist_app}'):
        fae.cluster.apps.action_deprecated(action=ActionConsts.UNINSTALL, param_name=ClusterConsts.APP_NAME, param_value=non_exist_app).verify_result(False)

    with allure.step('try to fetch cluster package'):
        nmx_package.action_fetch(path=new_path).verify_result()

    with allure.step(f'try to install nmx package without uninstall {filename} - should fail'):
        fae.cluster.package.files.file_name[filename].action_install(reboot_params=False, force=False).verify_result(False)

    with allure.step(f'verify old version'):
        ClusterTools.verify_app_version(fae.cluster, app_to_test, default_version)

    with allure.step(f'try to delete fetched file {filename}'):
        nmx_package.files.file_name[filename].action_delete().verify_result()

    with allure.step(f'try to delete already deleted fetched file {filename}'):
        nmx_package.files.file_name[filename].action_delete().verify_result(False)
