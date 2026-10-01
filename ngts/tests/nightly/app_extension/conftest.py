import pytest
import logging
import allure

from ngts.tests.nightly.app_extension.app_extension_helper import \
    verify_add_app_to_repo, verify_app_container_up_and_repo_status_installed, APP_INFO, app_cleanup, \
    get_app_repository
from ngts.constants.constants import InfraConst
from ngts.helpers.e1000_tx_watchdog_debug import collect_e1000_tx_watchdog_debug

logger = logging.getLogger()


@pytest.fixture(scope='function', autouse=False)
def e1000_tx_watchdog_debug(request, topology_obj, dumps_folder, is_simx, is_air):
    """
    Collect enhanced debug data for the intermittent e1000 mgmt-NIC TX-watchdog reset on SimX
    setups (Redmine #5040675). Three data streams are sampled in parallel for the whole test
    window: guest /proc/interrupts+/proc/softirqs, SimX-container QEMU 'info irq', and host
    'perf sched record'. Runs only on SimX (non-air) setups and never fails the test.
    """
    if not is_simx or is_air:
        yield
        return

    name_prefix = request.node.name.replace('/', '_').replace('[', '_').replace(']', '_')
    with collect_e1000_tx_watchdog_debug(topology_obj, dumps_folder, name_prefix):
        yield


@pytest.fixture(scope='function', autouse=False)
def add_app_into_repo(engines, cli_objects, is_air):
    """
    :param engines: ssh engines fixture

    """
    dut_engine = engines.dut
    app_name = APP_INFO["name"]
    app_repository_name = get_app_repository(is_air)
    version = APP_INFO["normal1"]["version"]
    app_cleanup(dut_engine, cli_objects.dut, app_name)
    cli_objects.dut.app_ext.add_repository(app_name, app_repository_name, version=version)
    verify_add_app_to_repo(cli_objects.dut, app_name, app_repository_name)

    yield dut_engine, app_name, version

    with allure.step('App package cleanup'):
        app_cleanup(dut_engine, cli_objects.dut, app_name)


@pytest.fixture(scope='function', autouse=False)
def pre_install_app(engines, cli_objects, add_app_into_repo):
    """
    :param engines: ssh engines fixture

    """
    dut_engine, app_name, version = add_app_into_repo
    with allure.step("Prerequisite: Install app with {}, version={}".format(app_name, version)):
        cli_objects.dut.app_ext.install_app(app_name, version)
        status = cli_objects.dut.general.get_container_status(app_name)
        assert not status, "Excepted container status is None, actual container status is {}".format(status)
        logger.info("Enable feature of {}".format(app_name))
        cli_objects.dut.app_ext.enable_app(app_name)
        verify_app_container_up_and_repo_status_installed(cli_objects.dut, app_name, version)

    yield dut_engine, version


@pytest.fixture(scope="package", autouse=True)
def skipping_app_ext_test_case(cli_objects):
    """
    If app ext feature is not ready, skipping all app ext test cases execution
    """
    if not cli_objects.dut.app_ext.verify_version_support_app_ext():
        pytest.skip("Skipping app ext test cases due to that app ext feature is not ready")


@pytest.fixture(scope='function', autouse=False)
def pre_install_base_image(topology_obj, cli_objects, upgrade_params, engines):
    """
    According to the upgrade_params, judge if do upgrade test.
    If do upgrade
    1. Install base image by deploy_image
    2. After test is over, need to recovery image to old image

    """
    if not upgrade_params.is_upgrade_required:
        pytest.skip('This platform not configure base and target version')
    dut_engine = engines.dut

    _, old_target_image = cli_objects.dut.general.get_base_and_target_images(dut_engine)
    base_version = upgrade_params.base_version
    target_version = upgrade_params.target_version
    if not base_version.startswith('http'):
        base_version = '{}{}'.format(InfraConst.HTTP_SERVER, base_version)
    if not target_version.startswith('http'):
        target_version = '{}{}'.format(InfraConst.HTTP_SERVER, target_version)
    with allure.step("Prerequisite: Install base image {}".format(base_version)):
        cli_objects.dut.general.deploy_image(topology_obj, base_version)

    yield base_version, target_version

    with allure.step("Recovery old target image {}".format(old_target_image)):
        current_base_version, current_target_version = cli_objects.dut.general.get_base_and_target_images(dut_engine)
        if old_target_image != current_target_version:
            if current_base_version == old_target_image:
                switch_version_by_set_default_image(topology_obj, engines.dut, cli_objects, target_version)
            else:
                cli_objects.dut.general.deploy_image(topology_obj, target_version)


def switch_version_by_set_default_image(topology_obj, dut_engine, cli_objects, version):
    """
    Switch version by setting default image and check if switch success or not
    """
    with allure.step("Set {} as default image".format(version)):
        cli_objects.dut.general.set_default_image(dut_engine, version)
    with allure.step('Rebooting the dut'):
        cli_objects.dut.general.safe_reboot_flow(topology_obj=topology_obj)
    with allure.step('Verifying dut booted with correct image'):
        # installer flavor might change after loading a different version
        delimiter = cli_objects.dut.general.get_installer_delimiter(dut_engine)
        image_list = cli_objects.dut.general.get_sonic_image_list(dut_engine, delimiter)
        assert 'Current: {}'.format(version) in image_list
    with allure.step("Verify basic container is up"):
        cli_objects.dut.general.verify_dockers_are_up(dut_engine)
