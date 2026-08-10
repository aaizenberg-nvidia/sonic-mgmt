import sys
import urllib.parse
import logging
import base64
import os
import random
import string
import pytest
import time

from devts.infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine

from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts, NvlInterfaceConsts
from ngts.nvos_tools.infra.InterfaceConfigurationTool import InterfaceConfigurationTool
from ngts.tests_nvos.general.security.centralized_tests.upgrade import test_upgrade  # TODO: we should't import stuff from other test files directly
from ngts.nvos_constants.constants_nvos import SystemConsts, NvosConst, ImageConsts
from ngts.scripts.sonic_deploy.nvos_only_methods import NvosInstallationSteps
from devts.infra.tools.general_constants.constants import DefaultConnectionValues
from ngts.tests_nvos.general.security import conftest as security_conftest  # TODO: we should't import stuff from conftest directly
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from ngts.ngts_types import DevicesT
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime, SubDuration
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.system import helpers as system_helpers
from ngts.nvos_tools.infra.IbRouterTool import IbRouterTool
from ngts.nvos_tools.infra.DutUtilsTool import RebootParams
from ngts.tests_nvos.checklist import test_checklist_ipv6  # TODO: we shouldn't import stuff from other test files directly
from ngts.tools.test_utils import allure_utils as allure, nvos_general_utils
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.actions.Actions import Action
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.Devices.BaseDevice import BaseDevice
from ngts.tests_nvos.system.gnmi.mapping.helpers import parse_gnmic_flat_output, run_gnmic_once_flat
from ngts.tests_nvos.system.reboot_telemetry_helpers import gnmi_client_for_dut
from ngts.tests_nvos.general.security.conftest import create_ssh_login_engine
from ngts.tools.test_utils.nvos_general_utils import check_partitions_capacity
from ngts.tests_nvos.general.security.centralized_tests.upgrade.test_upgrade import fetch_install_img
from ngts.tests_nvos.checklist.test_checklist_ipv6 import send_open_api_request
from ngts.tests_nvos.system.helpers import (
    extract_acl_rules,
    extract_control_plane_acl_bindings,
    verify_acl_rules_preserved,
    verify_control_plane_acl_bindings,
)
from ngts.nvos_constants.constants_nvos import OperationTimeConsts
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.RemoteAaaServerInfo import ping_server

from ngts.tests_nvos.constants import MINUTE


cumulus_owner = "bsrinivas"


logger = logging.getLogger(__name__)

PATH_TO_IMAGED_DIRECTORY = "/auto/sw_system_release/nos/nvos/"
PATH_TO_IMAGE_TEMPLATE = "{}/amd64/"

# To be uncommented when release is moved to next release - 25.01.4000
# BASE_IMAGE_VERSION_TO_INSTALL = "nvos-amd64-{pre_release_name}-001.bin"
# BASE_IMAGE_VERSION_TO_INSTALL_PATH = "/auto/sw_system_release/nos/nvos/{pre_release_name}-001/amd64/{base_image}"

BASE_IMAGE_VERSION_TO_INSTALL = "nvos-amd64-{pre_release_name}.bin"
BASE_IMAGE_VERSION_TO_INSTALL_PATH = "/auto/sw_system_release/nos/nvos/{pre_release_name}/amd64/{base_image}"


def _assert_dut_can_reach_sonic_mgmt_ipv6(dut_engine, sonic_mgmt_ipv6_addr):
    ipv6_addr = sonic_mgmt_ipv6_addr.strip("[]")
    if not IpTool.is_address_ipv6(ipv6_addr):
        raise AssertionError(f"Invalid sonic-mgmt IPv6 address for image fetch precheck: {sonic_mgmt_ipv6_addr}")

    with allure.step(f"Check DUT IPv6 connectivity to sonic-mgmt {ipv6_addr}"):
        is_reachable = ping_server(ipv6_addr, count=3, timeout=2, dut_engine=dut_engine)
        assert is_reachable, f"DUT cannot reach sonic-mgmt IPv6 address {ipv6_addr}; not running IPv6 image fetch."


def _delete_existing_image_files(system, image_names):
    image_names = sorted(set(image_names))
    if not image_names:
        logger.info("No fetched image files were recorded for cleanup")
        return

    existing_files = system.image.files.get_files()
    files_to_delete = [image_name for image_name in image_names if image_name in existing_files]
    missing_files = sorted(set(image_names) - set(files_to_delete))
    if missing_files:
        logger.info("Skipping cleanup for image files that are already missing: %s", missing_files)
    if not files_to_delete:
        logger.info("No recorded fetched image files still exist on the DUT")
        return

    delete_result = system.image.files.delete_files(files_to_delete)
    if not delete_result and "File not found" in delete_result.info:
        logger.warning(
            "Ignoring cleanup file-not-found result for image files: %s\n%s",
            files_to_delete,
            delete_result.info,
        )
        return
    delete_result.verify_result()


@pytest.fixture(scope='function', autouse=True)
def clear_system_image_files():
    """Clean up image files before and after each test."""
    system = System()
    with allure.step('clear all system image files before tests'):
        files = system.image.files.get_files()
        if files:
            logger.info(f"Cleaning up existing files: {list(files.keys())}")
            system.image.files.delete_files(files_to_delete=list(files.keys())).verify_result()
        else:
            logger.info("No files to clean up")

    yield  # run the test

    with allure.step('clear all system image files after tests'):
        try:
            system_after = System()
            files = system_after.image.files.get_files()
            if files:
                logger.info(f"Cleaning up after test: {list(files.keys())}")
                system_after.image.files.delete_files(files_to_delete=list(files.keys())).verify_result()
        except Exception as e:
            logger.warning("Cleanup after test failed (non-critical): %s", e)


@pytest.mark.checklist
@pytest.mark.nvos_ci
@pytest.mark.simx
@pytest.mark.image
@pytest.mark.system
@pytest.mark.nvos_build
@pytest.mark.cumulus
def test_show_system_image(original_version, engines, devices):
    """
    Show system image test

    Test flow:
    1. Run show system image
    2. Compare the current image value to the output from 'show system image current'
    3. Compare the output of 'show system image current' to 'show system version'
    4. Compare the output of 'show system image' to 'show system image installed'
    5. Compare the output of 'show system image' to 'show system image next'
    """
    system = System()
    with allure.step("Run show command to view system image"):
        show_output = system.image.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()

        with allure.step("Validate all expected fields in show output"):
            ValidationTool.verify_field_exist_in_json_output(output_dictionary,
                                                             [ImageConsts.CURRENT_IMG, ImageConsts.PARTITION1_IMG,
                                                              ImageConsts.NEXT_IMG]).verify_result()
            logger.info("All expected fields were found")

        with allure.step("Validate the values exist"):
            if ImageConsts.PARTITION2_IMG in output_dictionary.keys():
                partition2 = output_dictionary[ImageConsts.PARTITION2_IMG][ImageConsts.BUILD_ID]
            else:
                partition2 = ''
            current_partition = ImageConsts.PARTITION + output_dictionary[ImageConsts.CURRENT_IMG]
            current_version = output_dictionary[current_partition][ImageConsts.BUILD_ID]
            assert current_version == original_version, \
                f"Current image is invalid. Expected {original_version}"
            assert output_dictionary[ImageConsts.PARTITION1_IMG][ImageConsts.BUILD_ID] == original_version or partition2 == original_version, \
                f"Partition1 image is invalid. Expected {original_version}"
            assert output_dictionary[ImageConsts.NEXT_IMG] == output_dictionary[ImageConsts.CURRENT_IMG], \
                "Next image is not the current as expected in default settings."

        verify_system_image_gnmi_matches_nvue(engines, devices, system)

    with allure.step("Run show command to view system image files"):
        output_dictionary = system.image.files.get_files()
        if output_dictionary:
            for image_file, path_dict in output_dictionary.items():
                assert image_file in path_dict['path'], "The image file {} has the wrong path {}".format(image_file,
                                                                                                         path_dict[
                                                                                                             'path'])


@pytest.mark.checklist
@pytest.mark.simx
@pytest.mark.image
@pytest.mark.system
@pytest.mark.timeout(30 * MINUTE, func_only=True)
def test_downgrade_upgrade(release_name, random_api, original_version, devices, engines, downgrade_version_realpath,
                           target_version_realpath, dut_ipv6_addr, ib_router, topology_obj):
    """
    Check the image rename cmd.
    Validate that install and delete commands will success with the new name
    and will fail with the old name.
    1. Fetch random image
    2. Rename image
    3. Install original image name, should fail
    4. Delete the original image name , should fail
    5. Install new image name , success
    6. Uninstall image
    7. Delete the new image name , success
    """
    config_file_path = ''
    mtu_info = None
    json_acl_rules = None
    cp_acl_bindings = None
    acl_persist_mangle_state = None
    include_mtu_testing = getattr(devices.dut, 'supports_mtu_testing', False)

    if not downgrade_version_realpath:
        pytest.skip("Cannot run test because base_version parameter is missing from the setup file")

    orig_engine: LinuxSshEngine = TestToolkit.engines.dut
    system = System()
    verify_current_version(original_version, system, devices.dut)
    has_active_ports = InterfaceConfigurationTool.has_active_ports(devices.dut)

    if include_mtu_testing:
        logger.info(f"Active ports available for MTU testing: {has_active_ports}")

    original_images, _, original_image_partition, _, fetched_image = \
        get_image_data_and_fetch_base_image(system, downgrade_version_realpath)
    fetched_image_file = system.image.files.file_name[fetched_image]

    try:
        with allure.step("Rename image and verify"):
            new_name = RandomizationTool.get_random_string(20, ascii_letters=string.ascii_letters + string.digits)
            fetched_image_file.rename_and_verify(new_name)

        with allure.step("Install original image name, should fail"):
            logger.info("Install original image name: {}, should fail".format(fetched_image))
            system.image.files.file_name[fetched_image].action_file_install().verify_result(False)

        with allure.step("Delete original image name, should fail"):
            system.image.files.delete_files([fetched_image]).verify_result(False, "File not found")

        with allure.step('rename back to original name'):
            fetched_image_file.action_rename(fetched_image).verify_result()

        with allure.step('install the fetched image (after renamed back to original name)'):
            install_image_and_verify(orig_engine=orig_engine, image_name=fetched_image,
                                     original_images=original_images, system=system, release_name=release_name,
                                     test_name='test_downgrade_upgrade',
                                     devices=devices,
                                     operation_label=OperationTimeConsts.IMAGE_DOWNGRADE)
            verify_system_image_gnmi_matches_nvue(engines, devices, system)

            with allure.step('uninstall orig version'):
                system.image.action_uninstall('force')
            verify_system_image_gnmi_matches_nvue(engines, devices, system)

            with allure.step('run curl via ipv6, customer bug #4318552'):
                test_checklist_ipv6.send_open_api_request(dut_ipv6_addr, engines.dut)

        run_post_downgrade_cheks(ib_router)
        player = engines['sonic_mgmt']
        scp_host_creds = f'{player.username}:{player.password}@{player.ip}'
        with allure.step('Get config file and path for target version'):
            config_file_path, config_filename = devices.dut.get_test_config_file_by_version(original_version)

        with TestToolkit.force_api(ApiType.NVUE):
            mtu_info, _ = NvosInstallationSteps.setup_test_environment_with_config_and_speed(
                config_filename, config_file_path, engines, devices, system, scp_host_creds, engines.dut,
                include_mtu_testing=include_mtu_testing and has_active_ports and not ib_router, verify_result=True)

        # ACL + control-plane baseline after config is applied on downgraded image (before upgrade to target)
        with allure.step("Add ACL/control-plane configs for persistence checks (before baseline capture)"):
            acl_persist_mangle_state = system_helpers.add_acl_new_configs_for_persistence_checks(
                dut_engine=engines.dut)

        with allure.step("Save default ACL rules baseline (before upgrade to target image)"):
            json_acl_rules = system_helpers.extract_acl_rules(dut_engine=engines.dut)
            logger.info("ACL baseline captured for %d default ACL object(s)", len(json_acl_rules))

        with allure.step("Save control-plane ACL bindings baseline (nv show system control-plane acl)"):
            cp_acl_bindings = system_helpers.extract_control_plane_acl_bindings(
                dut_engine=engines.dut, mangle_state=acl_persist_mangle_state)
            logger.info("Control-plane ACL bindings captured: %s", cp_acl_bindings)

        logger.info("After replacing configuration file, system may ask for new password. Restoring password:")
        engines.dut.disconnect()
        engines.dut.run_cmd("true")

        run_pre_upgrade_steps(topology_obj, engines, ib_router)

    finally:
        target_fetched_image = None
        upgrade_succeeded = False
        try:
            with (allure.step(f"Run upgrade: {target_version_realpath}")):
                _, _, _, _, target_fetched_image = \
                    get_image_data_and_fetch_base_image(system, target_version_realpath)
                install_image_and_verify(orig_engine=TestToolkit.engines.dut,
                                         image_name=target_fetched_image,
                                         original_images=original_images, system=system,
                                         release_name=release_name,
                                         test_name='test_downgrade_upgrade',
                                         devices=devices,
                                         operation_label=OperationTimeConsts.IMAGE_UPGRADE)
                upgrade_succeeded = True

            with allure.step('Verify configuration preserved after upgrade'):
                run_post_upgrade_cheks(topology_obj, engines, ib_router)
        finally:
            with allure.step('cleanup test'):
                cleanup_test(
                    system, original_images, original_image_partition,
                    [f for f in dict.fromkeys([fetched_image, target_fetched_image]) if f],
                    config_file_path=config_file_path if upgrade_succeeded else '',
                    orig_engine=orig_engine,
                    target_version_realpath=target_version_realpath,
                    release_name=release_name,
                    test_name='test_downgrade_upgrade',
                    devices=devices,
                )

            if upgrade_succeeded and include_mtu_testing:
                with allure.step('Verify MTU preserved after upgrade'):
                    InterfaceConfigurationTool.verify_and_cleanup_mtu(mtu_info)

            if json_acl_rules is not None:
                with allure.independent_step('Verify default ACL JSON preserved after upgrade (nv show acl)'):
                    system_helpers.verify_acl_rules_preserved(
                        json_acl_rules, mangle_state=acl_persist_mangle_state, dut_engine=engines.dut)
            if cp_acl_bindings is not None:
                with allure.independent_step(
                    'Verify control-plane ACL bindings + loopback defaults on interface lo after upgrade'
                ):
                    system_helpers.verify_control_plane_acl_bindings(cp_acl_bindings, dut_engine=engines.dut)

        with allure.step('Clear ACL persistence mangle state (if any)'):
            system_helpers.clear_acl_configs(acl_persist_mangle_state, dut_engine=engines.dut)


@pytest.mark.checklist
@pytest.mark.simx
@pytest.mark.image
@pytest.mark.system
@pytest.mark.cumulus
def test_system_image_upload(engines, release_name, random_api, original_version, devices, downgrade_version_realpath):
    """
    Uploading image file to player and validate.
    1. Fetch random image
    2. Upload image to player
    3. Validate image uploaded to player
    4. Delete image file from player
    5. Delete image file from dut
    """
    system = System()

    verify_current_version(original_version, system, devices.dut)

    _, _, _, _, image_name = get_image_data_and_fetch_base_image(system, downgrade_version_realpath)
    image_file = system.image.files.file_name[image_name]
    player = engines['sonic_mgmt']
    uri_builders = {
        'scp': nvos_general_utils.generate_scp_uri_using_player,
        'sftp': nvos_general_utils.generate_sftp_uri_using_player,
    }
    upload_protocols = list(uri_builders.keys())

    try:
        with allure.step("Upload image to player {} with the next protocols : {}".format(player.ip, upload_protocols)):
            for protocol in upload_protocols:
                with allure.step("Upload image to player with {} protocol".format(protocol)):
                    upload_path = uri_builders[protocol](player, f'/tmp/{image_name}')
                    image_file.action_upload(upload_path).verify_result()

                with allure.step("Validate file was uploaded to player and delete it"):
                    assert player.run_cmd(
                        cmd='ls /tmp/ | grep {}'.format(image_name)), "Did not find the file with ls cmd"
                    player.run_cmd(cmd='rm -f /tmp/{}'.format(image_name))
    finally:
        with allure.step("Delete file from switch"):
            system.image.files.delete_files([image_name]).verify_result()
            system.image.files.verify_show_files_output(unexpected_files=[image_name])


@pytest.mark.checklist
@pytest.mark.simx
@pytest.mark.image
@pytest.mark.system
@pytest.mark.timeout(25 * MINUTE, func_only=True)
def test_image_uninstall(release_name, random_api, original_version, test_name, devices, downgrade_version_realpath):
    """
     Will check the uninstall commands

    Test flow:
    1. Validate that uninstall with 1 image only will fail
    2. Fetch and install an images
    3. Validate that uninstall will fail (because one is the current and the other is next-boot)
    4. Set the original image to be booted next
    5. Validate that uninstall will success
    """
    image_uninstall_test(release_name, original_version, devices, uninstall_force="", test_name=test_name,
                         base_version=downgrade_version_realpath)


@pytest.mark.checklist
@pytest.mark.simx
@pytest.mark.image
@pytest.mark.system
@pytest.mark.timeout(25 * MINUTE, func_only=True)
def test_image_uninstall_force(release_name, original_version, test_name, devices, downgrade_version_realpath):
    """
     Will check the uninstall force commands

    Test flow:
    1. Validate that uninstall force with 1 image only will fail
    2. Fetch and install force an images
    3. Validate that uninstall force success
    4. Set the original image to be booted next
    5. Validate that uninstall force will success
    """
    image_uninstall_test(release_name, original_version, devices, uninstall_force="force", test_name=test_name,
                         base_version=downgrade_version_realpath)


@pytest.mark.checklist
@pytest.mark.simx
@pytest.mark.image
@pytest.mark.system
@pytest.mark.timeout(7 * MINUTE, func_only=True)
def test_system_image_bad_flow(engines, release_name, random_api, original_version, sonic_mgmt_ipv6_addr,
                               downgrade_version_realpath):
    """
    Check bad flow scenarios:
    -	Fetch something that doesn't / already exist
    -	Delete something that doesn't exist
    -	Install something that doesn't exist
    -	Install the same current image
    -	Boot next something that doesn't / already exist
    -	Rename something that doesn't exist
    -	Upload image that doesn't / already exist

    """
    system = System()
    original_images, original_image, original_image_partition, other_partition = get_image_data(system)
    rand_name = RandomizationTool.get_random_string(10, ascii_letters=string.ascii_letters)
    file_rand_name = system.image.files.file_name[rand_name]

    with allure.step("Get an available image file"):
        _, _, _, _, image_name = get_image_data_and_fetch_base_image(system, downgrade_version_realpath)
        image_path = downgrade_version_realpath
        images_name = {image_name}
        image_file = system.image.files.file_name[image_name]

    try:
        with allure.step("Fetch bad flows"):
            with allure.independent_step("Fetch an image"):
                player = engines['sonic_mgmt']
                scp_path = ImageConsts.SCP_PATH_SERVER.format(username=player.username, password=player.password,
                                                              ip=player.ip, path=image_path)
                system.image.action_fetch(scp_path, base_url='').verify_result()
                images_name.add(image_name)

            if IpTool.is_dhcp_client6_has_lease(engines.dut):
                with allure.independent_step("Fetch the same image again using ipv6 address"):
                    _assert_dut_can_reach_sonic_mgmt_ipv6(engines.dut, sonic_mgmt_ipv6_addr)
                    ipv6_scp_path = ImageConsts.SCP_PATH_SERVER.format(username=player.username, password=player.password,
                                                                       ip=f"[{sonic_mgmt_ipv6_addr}]", path=image_path)
                    system.image.action_fetch(ipv6_scp_path, base_url='').verify_result()
                    scp_path = ipv6_scp_path

            with allure.independent_step("Fetch an image that does not exist"):
                system.image.action_fetch(scp_path + rand_name, base_url='').verify_result(False)

        with allure.step("Delete bad flows"):
            with allure.independent_step("Delete file that does not exist"):
                system.image.files.file_name[rand_name].action_delete().verify_result(False, "File not found")

        with allure.step("Install bad flows"):
            with allure.independent_step("Install image file that does not exist"):
                file_rand_name.action_file_install(expected_str="", force=True
                                                   ).verify_result(False, "Image does not exist")

        with allure.step("Boot-next bad flows"):
            if not original_images[ImageConsts.PARTITION2_IMG][ImageConsts.BUILD_ID]:
                with allure.independent_step(
                        f"Boot-next {ImageConsts.PARTITION2_IMG}, even though we have no image there"):
                    system.image.action_boot_next(ImageConsts.PARTITION2_IMG, f"No image on {ImageConsts.PARTITION2_IMG}").verify_result(False)
            with allure.independent_step("Boot-next random string"):
                system.image.action_boot_next(RandomizationTool.get_random_string(10), "Error").verify_result(False)
            with allure.independent_step("Boot-next the same partition (to revert any changes that may have happened)"):
                system.image.action_boot_next(original_image_partition)

        with allure.step("Rename bad flows"):
            with allure.step("Rename image file that does not exist"):
                file_rand_name.action_rename(rand_name).verify_result(False, "File not found")

        with allure.step("Upload bad flows"):
            player = engines['sonic_mgmt']
            upload_path = ImageConsts.SCP_PATH_SERVER.format(username=player.username, password=player.password, ip=IpTool.format_ip_for_uri(player), path='/tmp')
            with allure.independent_step("Upload image file that does not exist"):
                file_rand_name.action_upload(upload_path).verify_result(False, "File not found")
            with allure.independent_step("Upload the same image twice"):
                with allure.step("First upload"):
                    image_file.action_upload(upload_path).verify_result()
                    with allure.step("Validate file was uploaded"):
                        assert player.run_cmd(
                            cmd='ls /tmp/ | grep {}'.format(image_name)), "Did not find the file with ls cmd"
                with allure.step("Second upload"):
                    image_file.action_upload(upload_path).verify_result()
                    with allure.step("Delete the file from the player"):
                        player.run_cmd(cmd='rm -f /tmp/{}'.format(image_name))
    finally:
        with allure.step("Delete all images that have been fetch during the test"):
            _delete_existing_image_files(system, images_name)


@pytest.mark.checklist
@pytest.mark.image
@pytest.mark.system
def test_install_multiple_images(release_name, test_name, random_api, original_version, devices, engines):
    """
    Install system image test

    1. Fetch 2 random images, Verify fetched images are listed in the show image files output
    5. Install image <img_1>, Verify installed images are listed in the show images
    6. Check available partitions capacity
    7. Set the original image to boot next
    8. Reboot dut and make sure it boots with original image
    9. Uninstall all images that have been installed during the test
    10. Delete all images that have been fetched during the test
    """
    with allure.step(f"Update path with provided release name: {release_name}"):
        global BASE_IMAGE_VERSION_TO_INSTALL
        BASE_IMAGE_VERSION_TO_INSTALL = BASE_IMAGE_VERSION_TO_INSTALL.format(pre_release_name=release_name)
        logger.info(f"base image name: {BASE_IMAGE_VERSION_TO_INSTALL}")

        global BASE_IMAGE_VERSION_TO_INSTALL_PATH
        BASE_IMAGE_VERSION_TO_INSTALL_PATH = BASE_IMAGE_VERSION_TO_INSTALL_PATH.format(pre_release_name=release_name,
                                                                                       base_image=BASE_IMAGE_VERSION_TO_INSTALL)
        logger.info(f"base image path: {BASE_IMAGE_VERSION_TO_INSTALL_PATH}")

    system = System()

    verify_current_version(original_version, system, devices.dut)

    original_images, original_image, original_image_partition, _, image_files = \
        get_image_data_and_fetch_random_image_files(release_name, system, 1)

    with allure.step("Verify fetched images are shown in the show command"):
        system.image.files.verify_show_files_output(expected_files=image_files)

    with allure.step("Verify show images output didn't change after the fetch command"):
        system.image.verify_show_images_output(original_images)

    with allure.step("Fetch the second image"):
        player = TestToolkit.engines['sonic_mgmt']
        scp_path = 'scp://{}:{}@{}'.format(player.username, player.password, player.ip)

        with allure.step("Fetch an image {}".format(scp_path + BASE_IMAGE_VERSION_TO_INSTALL_PATH)):
            system.image.action_fetch(BASE_IMAGE_VERSION_TO_INSTALL_PATH, scp_path)
            image_files.append(
                BASE_IMAGE_VERSION_TO_INSTALL) if BASE_IMAGE_VERSION_TO_INSTALL not in image_files else image_files

    try:
        with allure.step("Install the first image"):
            orig_engine: LinuxSshEngine = TestToolkit.engines.dut
            install_image_and_verify(orig_engine, BASE_IMAGE_VERSION_TO_INSTALL,
                                     original_images, system, release_name, test_name, devices=devices)
            verify_system_image_gnmi_matches_nvue(engines, devices, system)
        with allure.step("Test partitions available capacity"):
            nvos_general_utils.check_partitions_capacity(allowed_limit=60)

    finally:
        cleanup_test(system, original_images, original_image_partition, image_files, orig_engine=orig_engine)


def image_uninstall_test(release_name, original_version, devices, uninstall_force="", test_name="", base_version=''):
    """
     Will check the uninstall commands
     for uninstall force command , the uninstall_force param need to get "force"

    Test flow:
    1. Validate that uninstall [force] with 1 image only will fail
    2. Fetch and install an images
    3. Validate that uninstall will fail (because one is the current and the other is next-boot),
        but uninstall force success
        3.1. if we check the force command so we will install the new image again
    4. Set the original image to be booted next
    5. Validate that uninstall [force] will success
    """
    if not base_version:
        pytest.skip("Cannot run test because base_version parameter is missing from the setup file")

    system = System()

    verify_current_version(original_version, system, devices.dut)

    original_images, _, original_image_partition, partition_id_for_new_image, fetched_image = \
        get_image_data_and_fetch_base_image(system, base_version)

    if original_images[partition_id_for_new_image][ImageConsts.BUILD_ID]:
        with allure.step("uninstall image, while there are 2 images- should success"):
            system.image.action_uninstall(params="force")
            image_output = system.image.get_image_field_values()
            assert not image_output[ImageConsts.PARTITION2_IMG][ImageConsts.BUILD_ID], "uninstall didn't work"

    else:
        with allure.step("{} uninstall image, while there is just 1 image- should fail".format(uninstall_force)):
            output = system.image.action_uninstall(params=uninstall_force, expected_str="Nothing to uninstall",
                                                   verify_res=False)
            assert "Nothing to uninstall" in output
            system.image.verify_show_images_output(original_images)

    try:
        with allure.step("Install image and verify"):
            orig_engine: LinuxSshEngine = TestToolkit.engines.dut
            install_image_and_verify(orig_engine, fetched_image, original_images, system,
                                     release_name, test_name, devices=devices)

            with allure.step("Set the original image to be booted next and verify"):
                system.image.boot_next_and_verify(original_image_partition)

        if not uninstall_force:
            output = system.image.action_uninstall(expected_str="Failed to uninstall. Image set to boot-next",
                                                   verify_res=False)
            assert "Failed to uninstall. Image set to boot-next" in output

    finally:
        cleanup_test(system, original_images, original_image_partition, [fetched_image], orig_engine=orig_engine)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.image
@pytest.mark.cumulus
def test_system_image_install_reject_with_smallcase_n(engines, original_version, devices):
    """
    Check the image install cmd by rejecting the prompt with 'n'
    Validate that install image command will be aborted when the prompt is rejected.
    1. Extract original image name
    2. Attempt image install command, reject the prompt with 'n'
    3. Check the image is the original one
    """
    system = System()
    prompt_response = 'n'
    system_image_install_reject_with_prompt(engines, system, prompt_response, original_version, devices)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.image
@pytest.mark.cumulus
def test_system_image_install_reject_with_uppercase_n(engines, original_version, devices):
    """
    Check the image install cmd by rejecting the prompt with 'N'
    Validate that install image command will be aborted when the prompt is rejected.
    1. Extract original image name
    2. Attempt image install command, reject the prompt with 'N'
    3. Check the image is the original one
    """
    system = System()
    prompt_response = 'N'
    system_image_install_reject_with_prompt(engines, system, prompt_response, original_version, devices)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.image
@pytest.mark.cumulus
def test_system_image_install_reject_with_random_char(engines, original_version, devices):
    """
    Check the image install cmd by rejecting the prompt with random character
    Validate that install image command will be aborted when the prompt is rejected.
    1. Extract original image name
    2. Attempt image install command, reject the prompt with random character
    3. Check the image is the original one
    """
    system = System()
    prompt_response = 't'
    system_image_install_reject_with_prompt(engines, system, prompt_response, original_version, devices)


def system_image_install_reject_with_prompt(engines, system, prompt_response, original_version, devices):
    verify_current_version(original_version, system, devices.dut)

    action_job_id = 0
    try:
        with allure.step("Create SSH Engine to login to the switch"):
            child = security_conftest.create_ssh_login_engine(engines.dut.ip, SystemConsts.DEFAULT_USER_ADMIN)
            assert isinstance(child.pid, int), "SSH login process failed to be spawned"
            respond = child.expect([DefaultConnectionValues.PASSWORD_REGEX, '~'])
            assert respond == 0, "SSH Connection to switch failed"
            child.sendline(engines.dut.password)
            respond = child.expect(DefaultConnectionValues.DEFAULT_PROMPTS[0])
            output = child.after.decode('utf-8')
            assert respond == 0, "Password prompt did not come up {out}".format(out=output)

        with allure.step("Extract Image name before attempting installing new image"):
            image_name = system.version.get_nvos_image_version()

        with allure.step("Attempt install image and reject the prompt"):
            # Get the last action-job-id
            exempted_err_msgs = ['action_error', 'File not found', 'Failed to install', 'Action failed']
            action = Action()
            output = OutputParsingTool.parse_json_str_to_dictionary(action.show(exempted_err_msgs=exempted_err_msgs)). \
                get_returned_value()
            if output:
                action_job_id = max([int(id_no) for id_no in list(output)])

            # Since the install is to be aborted, using a dummy image name nvos.bin
            child.sendline('nv action install system image files nvos.bin')
            respond = child.expect('.*continue.*')
            assert respond == 0, "Install image confirmation prompt did not come up"
            child.sendline(prompt_response)
            respond = child.expect('.*abort.*')
            assert respond == 0, "Image install abort message did not appear"

        with allure.step("Verify install command was executed successfully"):
            # Increment action-job-id for latest command status
            action_job_id_str = str(action_job_id + 1)
            # extract last command execution status
            output = OutputParsingTool.parse_json_str_to_dictionary(action.show(action_job_id_str)). \
                get_returned_value()
            assert output['detail'] == 'Image install aborted by user' and \
                output['http_status'] == 200 and \
                output['state'] == 'action_success', "Image install command failed:{out}".format(out=output)

        with allure.step("Verify image is unchanged"):
            image_name_post = system.version.get_nvos_image_version()
            assert image_name == image_name_post, "Image name changed even though image install command was aborted"

    finally:
        # close connection
        child.close()


@pytest.mark.checklist
@pytest.mark.image
@pytest.mark.system
@pytest.mark.cumulus
def test_fetch_image_via_https(test_api, target_version):
    """
    Install system image test

    1. Get the details of the image to be fetched based on release
    2. Fetch the image using nv action fetch system image https<>
    3. Verify the fetched image is shown in 'nv show system image files' output
    4. Delete the images that has been fetched during the test
    5. Verify the earlier fetched image does not appear in show command
    """
    system = System()
    image_fetched = False
    if target_version:
        image_to_fetch = os.path.realpath(target_version)
    else:
        image_file = TestToolkit.devices.dut.get_base_image()
        image_to_fetch = TestToolkit.devices.dut.get_base_image_path(image_file)
    image_file = os.path.basename(image_to_fetch)

    try:
        with allure.step("Fetch an image {}".format(image_to_fetch)):
            system.image.action_fetch(image_to_fetch, base_url=TestToolkit.devices.dut.nfs_server).verify_result()
            image_fetched = True

        with allure.step("Verify fetched image is shown in the show command"):
            system.image.files.verify_show_files_output(expected_files=[image_file])

    finally:
        if image_fetched:
            with allure.step("Delete the image that has been fetched during the test"):
                system.image.files.delete_files([image_file]).verify_result()

            with allure.step("Verify earlier fetched image is not shown in the show command"):
                system.image.files.verify_show_files_output(unexpected_files=[image_file])


@pytest.mark.checklist
@pytest.mark.image
@pytest.mark.system
def test_fetch_image_with_weird_password(random_api, engines):
    """
    Install system image test

    1. Create a dummy image to be fetched
    2. Create a user
    3. Set the password of the new user to some weird password
    4. Fetch the dummy image using the new user and password
    5. Confirm the image was successfully fetched using nv show
    6. Delete the image that has been fetched during the test
    7. Delete the use which was created during the test
    8. Delete the dummy image file created
    """
    system = System()
    # Create 5 passwords including 5 random special characters from the allowed special character list
    special_char_list = ['~', '@', '%', '^', '*', '_', '=', '+', '{', '}', ':', ',', '[', ']', '/', '!', "'"]
    weird_passwords = [("Password1" + special_char) for special_char in (random.sample(special_char_list, 5))]

    with allure.step("Delete all the pre existing images"):
        system.image.files.delete_all_existing_files()

    with allure.step("Create dummy file to be fetched"):
        cmd_to_create_file = "touch " + SystemConsts.DUMMY_IMAGE_PATH + SystemConsts.DUMMY_IMAGE
        engines.dut.run_cmd(cmd_to_create_file)

    for weird_password in weird_passwords:
        logger.info("Testing with password: {}".format(weird_password))
        helper_fetch_image_with_weird_password(engines, system, random_api, weird_password)

    with allure.step("Remove the dummy file"):
        cmd_to_remove_file = "rm " + SystemConsts.DUMMY_IMAGE_PATH + SystemConsts.DUMMY_IMAGE
        engines.dut.run_cmd(cmd_to_remove_file)


def helper_fetch_image_with_weird_password(engines, system, test_api, weird_password):
    new_user = ""
    image_fetched = False

    # For nv action fetch command, passwords with special chars need to be url encoded
    if "'" in weird_password:
        # Adding exception for apostrophe
        if test_api == ApiType.NVUE:
            weird_password = "Password1\\'"
        weird_password_urlencoded = weird_password
    else:
        weird_password_urlencoded = urllib.parse.quote(weird_password, safe='')

    if test_api == ApiType.OPENAPI:
        # encode password to base64 object and convert the base64 object to string
        weird_password = base64.b64encode(str.encode(weird_password)).decode()

    try:
        with allure.step("Create a new user with the weird password"):
            new_user, new_password = system.aaa.user.set_new_user(password=weird_password,
                                                                  role=SystemConsts.ROLE_VIEWER, apply=True)

        with allure.step("Fetch the dummy image {} using the new user and weird password".format(
                SystemConsts.DUMMY_IMAGE)):
            hostname = engines.dut.run_cmd('hostname')
            scp_path = ImageConsts.SCP_PATH_SERVER.format(username=new_user,
                                                          password=weird_password_urlencoded, ip=hostname,
                                                          path=SystemConsts.DUMMY_IMAGE_PATH + SystemConsts.DUMMY_IMAGE)
            system.image.action_fetch(scp_path, base_url='')
            image_fetched = True

        with allure.step("Verify fetched image is shown in the show command"):
            system.image.files.verify_show_files_output(expected_files=[SystemConsts.DUMMY_IMAGE])

    finally:
        if image_fetched:
            with allure.step("Delete the image that has been fetched during the test"):
                system.image.files.delete_files([SystemConsts.DUMMY_IMAGE]).verify_result()

            with allure.step("Verify earlier fetched image is not shown in the show command"):
                system.image.files.verify_show_files_output(unexpected_files=[SystemConsts.DUMMY_IMAGE])

        if new_user:
            with allure.step("Delete the newly created user"):
                system.aaa.user.user_id[new_user].unset(apply=True).verify_result()


# Minimum NVOS for system/image gNMI parity checks (25.02.8000 in BaseDevice._version_to_global_build pre-25.03 scale).
_MIN_GLOBAL_BUILD_SYSTEM_IMAGE_GNMI = 8000


def _dut_product_release_version(devices, engines, system) -> str:
    getter = getattr(devices.dut, '_get_system_version', None)
    if callable(getter):
        v = getter(engines.dut)
        if v:
            return str(v).strip()
    ver_out = OutputParsingTool.parse_json_str_to_dictionary(
        system.version.show(dut_engine=engines.dut)
    ).get_returned_value()
    return str((ver_out or {}).get(SystemConsts.VERSION_PRODUCT_RELEASE, '') or '').strip()


def _system_image_gnmi_leaf(client, path: str) -> str:
    path = path.lstrip('/')
    result = run_gnmic_once_flat(path, client=client)
    out = result[0] if isinstance(result, tuple) else result
    val = parse_gnmic_flat_output(out)
    assert val is not None, f"gNMI returned no value for {path}. raw output: {out!r}"
    return val


def _nvue_gnmi_values_equal(nvue_val, gnmi_val) -> bool:
    if nvue_val is None or nvue_val == '':
        return gnmi_val is None or str(gnmi_val).strip() == ''
    return str(nvue_val).strip() == str(gnmi_val).strip()


def _partition_slot_has_image(partition_data) -> bool:
    if isinstance(partition_data, dict):
        return bool(partition_data.get(ImageConsts.BUILD_ID))
    if isinstance(partition_data, str):
        return bool(partition_data.strip())
    return False


def verify_system_image_gnmi_matches_nvue(engines, devices, system=None):
    """Assert gNMI leaves under system/image/partitions match `nv show system image`.

    Skipped on NVOS older than 25.02.8000 (see ``BaseDevice._version_to_global_build``).
    """
    with allure.step("Verify system image gNMI matches NVUE"):
        with allure.independent_step("Compare gNMI system/image leaves to NVUE"):
            system = system or System()
            release = _dut_product_release_version(devices, engines, system)
            global_build = BaseDevice._version_to_global_build(release)
            if global_build < _MIN_GLOBAL_BUILD_SYSTEM_IMAGE_GNMI:
                logger.info(
                    "Skipping gNMI vs NVUE system image check: product-release %r global_build=%s "
                    "(need >= %s, i.e. 25.02.8000+ on pre-25.03 numbering).",
                    release,
                    global_build,
                    _MIN_GLOBAL_BUILD_SYSTEM_IMAGE_GNMI,
                )
                return

            nvue = OutputParsingTool.parse_json_str_to_dictionary(system.image.show()).get_returned_value()
            client = gnmi_client_for_dut(engines.dut, devices.dut)

            for rel_path, nvue_key in (
                ('system/image/partitions/state/current', ImageConsts.CURRENT_IMG),
                ('system/image/partitions/state/next', ImageConsts.NEXT_IMG),
            ):
                gnmi_v = _system_image_gnmi_leaf(client, rel_path)
                nvue_v = nvue.get(nvue_key)
                assert _nvue_gnmi_values_equal(nvue_v, gnmi_v), (
                    f"gNMI {rel_path}={gnmi_v!r} does not match NVUE {nvue_key}={nvue_v!r}"
                )

            part_to_id = (
                (ImageConsts.PARTITION1_IMG, '1'),
                (ImageConsts.PARTITION2_IMG, '2'),
            )
            state_leaves = (
                ('build-id', ImageConsts.BUILD_ID),
                ('description', NvosConst.DESCRIPTION),
                ('disk', SystemConsts.DISK),
                ('release', 'release'),
            )
            for part_key, pid in part_to_id:
                if part_key not in nvue:
                    continue
                pdata = nvue[part_key]
                if not _partition_slot_has_image(pdata):
                    continue

                id_path = f'system/image/partitions/partition[id={pid}]/id'
                gnmi_id = _system_image_gnmi_leaf(client, id_path)
                assert _nvue_gnmi_values_equal(pid, gnmi_id), (
                    f"gNMI {id_path}={gnmi_id!r} does not match expected partition id {pid!r}"
                )

                if isinstance(pdata, dict):
                    for leaf, nkey in state_leaves:
                        if nkey not in pdata:
                            continue
                        spath = f'system/image/partitions/partition[id={pid}]/state/{leaf}'
                        gnmi_v = _system_image_gnmi_leaf(client, spath)
                        nvue_v = pdata[nkey]
                        assert _nvue_gnmi_values_equal(nvue_v, gnmi_v), (
                            f"gNMI {spath}={gnmi_v!r} does not match NVUE {part_key}.{nkey}={nvue_v!r}"
                        )
                elif isinstance(pdata, str):
                    bid_path = f'system/image/partitions/partition[id={pid}]/state/build-id'
                    gnmi_bid = _system_image_gnmi_leaf(client, bid_path)
                    assert _nvue_gnmi_values_equal(pdata, gnmi_bid), (
                        f"gNMI {bid_path}={gnmi_bid!r} does not match NVUE {part_key}={pdata!r}"
                    )


def normalize_image_name(image_name):
    return image_name.replace("-amd64", "").replace(".bin", "")


def install_image_and_verify(
    orig_engine: ProxySshEngine,
    image_name: str,
    partition_id: str,
    original_images: dict,
    system: System,
    release_name: str,
    test_name: str = '',
    devices: DevicesT | None = None,
    *,
    operation_label: str = OperationTimeConsts.IMAGE_INSTALL,
) -> None:
    with allure.step("Installing image {}".format(image_name)):
        new_engine = LinuxSshEngine(
            orig_engine.ip,
            orig_engine.username,
            orig_engine.password,
            ssh_port=orig_engine.ssh_port,
            xml_rpc_port=orig_engine.xml_rpc_port,
            retry=getattr(orig_engine, "_retry", True),
            device_type=orig_engine.device_type,
            engine_connect_retries=orig_engine.engine_connect_retries,
            is_on_air=orig_engine.is_on_air,
        )
        with OperationTime.capture_sub_durations() as sub_durations:
            res_obj, _ = OperationTime.save_duration(operation_label, '', test_name,
                                                     system.image.files.file_name[image_name].action_file_install_with_reboot,
                                                     expected_str=SystemConsts.REBOOT_RESPONSE_MESSAGES,
                                                     force=True, recovery_engine=new_engine
                                                     )
        res_obj.verify_result()

        operation_duration = sub_durations.get(SubDuration.OPERATION)
        if operation_duration is not None and devices is not None:
            # Threshold key is the canonical one registered by device classes
            # (e.g. IbSwitch._init_expected_operation_durations)
            OperationTime.verify_operation_time(
                operation_duration, OperationTimeConsts.IMAGE_INSTALL_OPERATION_ROW, devices
            ).verify_result()

    with allure.step('replace dut engine'):
        TestToolkit.engines.dut = new_engine  # if install succeeded, need to replace dut engine

    with allure.step("Verify installed image"):
        time.sleep(5)

        image_output = system.image.get_image_field_values()
        image_name = normalize_image_name(image_name)
        res_obj = ValidationTool.verify_expected_output(system.image.show(), ImageConsts.BUILD_ID)
        res_obj.ignore_result()
        _, _, partition_id, _ = get_image_data(system)
        if res_obj.result:  # temp solution until 3000 GA
            with allure.step(f"Verify image was installed properly on {partition_id}"):
                assert image_output[partition_id][ImageConsts.BUILD_ID] == image_name, \
                    f"{image_name} was expected to be installed on {partition_id} but it failed"

            with allure.step("Verify current and next fields point to new image"):
                num = "1" if partition_id == ImageConsts.PARTITION1_IMG else "2"
                assert image_output[ImageConsts.NEXT_IMG] == image_output[ImageConsts.CURRENT_IMG] == num, \
                    "Next image is not the current as expected in default settings."
        else:
            with allure.step(f"Verify image was installed properly on {partition_id}"):
                assert image_output[partition_id] == image_name, \
                    f"{image_name} was expected to be installed on {partition_id} but it failed"

            with allure.step("Verify current and next fields point to new image"):
                assert image_output[ImageConsts.CURRENT_IMG] == image_name, \
                    "Current image is not as expected in default settings."


def get_list_of_directories(current_installed_img, starts_with=None):
    def mtime(f):
        return os.stat(os.path.join(PATH_TO_IMAGED_DIRECTORY, f)).st_mtime

    temp_directories = [dev for dev in os.listdir(PATH_TO_IMAGED_DIRECTORY) if "lastrc" not in str(dev)]
    temp_directories = list(sorted(temp_directories, key=mtime))

    all_directories = list(directory for directory in temp_directories if directory.startswith(starts_with))
    # above line will be replaced with below line once merged to develop
    # all_directories = list(directory for directory in temp_directories if directory.startswith(starts_with + "-"))

    all_directories.reverse()
    return_directories = {}
    for directory in all_directories:
        temp_dir = PATH_TO_IMAGED_DIRECTORY + PATH_TO_IMAGE_TEMPLATE.format(directory)
        if os.path.isdir(temp_dir) and "-001" not in temp_dir:
            logger.info("Searching for images in path: " + temp_dir)
            relevant_images = [
                f for f in os.listdir(temp_dir)
                if f.startswith("nvos-amd64-25.") and  # noqa: W504
                list(current_installed_img.values())[0].replace("nvos-25", "nvos-amd64-25") not in f
            ]
            if relevant_images:
                return_directories[temp_dir] = relevant_images
        if len(return_directories) == 2:
            break
    return return_directories


def get_images_to_fetch(release_name, current_installed_img, images_amount=1):
    images_to_fetch = []
    with allure.step("Get list of images"):
        relevant_directories = get_list_of_directories(current_installed_img, release_name)
        for directory, images_list in relevant_directories.items():
            if len(images_to_fetch) == images_amount:
                break
            images_to_fetch.append((images_list[0], directory + images_list[0]))
            logger.info("Selected image: " + directory + images_list[0])

    return images_to_fetch


def get_next_partition_id(partition_id):
    return ImageConsts.PARTITION2_IMG if partition_id == ImageConsts.PARTITION1_IMG else ImageConsts.PARTITION1_IMG


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


def _get_running_image(system):
    images = system.image.get_image_field_values()
    res_obj = ValidationTool.verify_expected_output(system.image.show(), ImageConsts.BUILD_ID)
    res_obj.ignore_result()
    if res_obj.result:
        current_partition = ImageConsts.PARTITION + images[ImageConsts.CURRENT_IMG]
        return normalize_image_name(images[current_partition][ImageConsts.BUILD_ID])
    return normalize_image_name(images[ImageConsts.CURRENT_IMG])


def _restore_original_image(
    system,
    original_images,
    original_image_partition,
    orig_engine=None,
    target_version_realpath='',
    release_name='',
    test_name='',
    devices=None,
):
    expected_image = normalize_image_name(original_images[original_image_partition][ImageConsts.BUILD_ID])
    if _get_running_image(system) == expected_image:
        logger.info("DUT already running original image (%s), skipping restore", expected_image)
        return

    if target_version_realpath:
        image_name = target_version_realpath.split("/")[-1]
        with allure.step(f"Install original image {image_name} to restore pre-test state"):
            fetched_files = system.image.files.get_files() or {}
            if image_name not in fetched_files:
                system.image.action_fetch(path=target_version_realpath).verify_result()
            install_image_and_verify(
                orig_engine=orig_engine or TestToolkit.engines.dut,
                image_name=image_name,
                original_images=original_images,
                system=system,
                release_name=release_name,
                test_name=test_name,
                devices=devices,
                operation_label=OperationTimeConsts.IMAGE_UPGRADE,
            )
        return

    with allure.step("Set the original image to be booted next and verify"):
        system.image.boot_next_and_verify(original_image_partition)

    with allure.step("Reboot the system"):
        system.action_reboot(reboot_params=RebootParams(recovery_engine=orig_engine))

    with allure.step('restore original dut engine'):
        TestToolkit.engines.dut = orig_engine or TestToolkit.engines.dut


def cleanup_test(
    system,
    original_images,
    original_image_partition,
    fetched_image_files,
    config_file_path='',
    orig_engine=None,
    target_version_realpath='',
    release_name='',
    test_name='',
    devices=None,
):
    with allure.step("Cleanup step"):
        configuration_diff = {}
        restore_error = None
        try:
            _restore_original_image(
                system, original_images, original_image_partition,
                orig_engine=orig_engine, target_version_realpath=target_version_realpath,
                release_name=release_name, test_name=test_name, devices=devices,
            )
        except Exception as e:
            logger.exception("Image restore failed during cleanup")
            restore_error = e

        if config_file_path:
            with allure.step('Verify configuration was preserved after upgrade'):
                configuration_diff = NvosInstallationSteps.verify_config_after_upgrade(
                    config_file_path, TestToolkit.engines.dut)

        with allure.step("Uninstall unused images and verify"):
            try:
                system.image.action_uninstall(params='force')
                system.image.verify_show_images_output(original_images)
            except Exception:
                logger.info("No image to uninstall")

        with allure.step("Delete all images that have been fetch during the test and verify"):
            _delete_existing_image_files(system, fetched_image_files)
            system.image.files.verify_show_files_output(unexpected_files=sorted(set(fetched_image_files)))

        # Restore failed: surface it, unless a test exception is already propagating (don't mask it)
        if restore_error is not None and sys.exc_info()[0] is None:
            raise restore_error

        if configuration_diff:
            import json
            diff_pretty = json.dumps(configuration_diff, indent=2, default=str)
            missing_keys = _extract_leaf_paths(configuration_diff)
            summary = "\n".join(f"  - {path}" for path in missing_keys)
            assert False, (
                f"Configuration was not preserved across image upgrade.\n"
                f"Missing/mismatched settings ({len(missing_keys)}):\n{summary}\n\n"
                f"Full diff:\n{diff_pretty}"
            )


def get_image_data(system) -> tuple[dict, str, str, str]:
    """
    Returns: Output of nv show system image (as dict),
             name of the image in the current partition,
             name of the currently active partition (partition1/2),
             name of the other partition.
    """
    with allure.step("Save original installed image name"):
        original_images = system.image.get_image_field_values()
        current_partition = ImageConsts.PARTITION + original_images[ImageConsts.CURRENT_IMG]
        original_image = original_images[current_partition][ImageConsts.BUILD_ID]
        partition_id_for_new_image = get_next_partition_id(current_partition)
        logger.info("Original image: {}, partition: {}".format(original_image, current_partition))
        return original_images, original_image, current_partition, partition_id_for_new_image


def get_image_data_and_fetch_random_image_files(release_name, system, images_amount_to_fetch=1):
    original_images, original_image, original_image_partition, partition_id_for_new_image = get_image_data(system)

    with allure.step("Get {} available image files".format(images_amount_to_fetch)):
        images_to_fetch = get_images_to_fetch(release_name, original_image, images_amount_to_fetch)
        images_name = []
        for image_name, image_path in images_to_fetch:
            player = TestToolkit.engines['sonic_mgmt']
            scp_path = 'scp://{}:{}@{}'.format(player.username, player.password, player.ip)
            with allure.step("Fetch an image {}".format(scp_path + image_path)):
                system.image.action_fetch(image_path, base_url=scp_path)
                images_name.append(image_name)
    return original_images, original_image, original_image_partition, partition_id_for_new_image, images_name


def get_image_data_and_fetch_base_image(system, base_version):
    original_images, original_image, original_image_partition, partition_id_for_new_image = get_image_data(system)

    with allure.step(f"Fetch image {base_version}"):
        _ = TestToolkit.engines['sonic_mgmt']
        system.image.action_fetch(path=base_version).verify_result()
    image_name = base_version.split("/")[-1]
    return original_images, original_image, original_image_partition, partition_id_for_new_image, image_name


def _choose_random_port_and_test_speed_configuration(engines, devices):
    """
    Wrapper function that delegates to InterfaceConfigurationTool for speed testing.

    This function maintains backward compatibility while using the new generic
    InterfaceConfigurationTool for actual speed testing logic.
    """
    return InterfaceConfigurationTool.choose_random_port_and_test_speed_configuration(engines, devices)


def _detect_system_type_and_select_port(device):
    """
    Detect system type and select a random ACTIVE/CONNECTED port for speed testing.

    This function ensures that only ports with active links are selected for speed testing,
    preventing failures due to disconnected interfaces. It follows the same pattern as
    _get_available_nvl_ports to validate link status before selection.
    """
    if hasattr(device, 'interface_list') and device.interface_list:
        with allure.step("IB system detected - choosing ACTIVE port from interface_list"):
            # Select only ports that are UP and ACTIVE (like _get_available_nvl_ports does)
            try:
                selected_port_obj = RandomizationTool.select_random_port(
                    requested_ports_state=NvosConsts.LINK_STATE_UP,
                    requested_ports_logical_state=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE,
                    interface_type=NvlInterfaceConsts.SW_INTERFACE_TYPE
                ).get_returned_value()
                port_name = selected_port_obj.name
                logger.info(f"Selected ACTIVE IB port for speed testing: {port_name}")
                return NvosConst.IB_SWITCH_TYPE, selected_port_obj, port_name
            except Exception as e:
                logger.error(f"Failed to find active IB port: {e}")
                pytest.skip("No active IB ports available for speed testing")

    elif hasattr(device, NvosConst.NVL_ACCESS_PORTS_LIST) or hasattr(device, NvosConst.NVL_TRUNK_PORTS_LIST):
        with allure.step("NVL system detected - choosing ACTIVE port from available nvl port types"):
            # First, determine what port types are available
            available_port_types = []

            if hasattr(device, 'nvl_trunk_ports_list') and device.nvl_trunk_ports_list:
                available_port_types.append('trunk')
                logger.info(f"NVL trunk ports available: {len(device.nvl_trunk_ports_list)} ports")

            if hasattr(device, 'nvl_access_ports_list') and device.nvl_access_ports_list:
                available_port_types.append('access')
                logger.info(f"NVL access ports available: {len(device.nvl_access_ports_list)} ports")

            if not available_port_types:
                pytest.skip("No NVL ports available for speed testing")

            # Randomly choose between available port types
            chosen_port_type = RandomizationTool.select_random_value(available_port_types).get_returned_value()
            logger.info(f"Randomly chosen NVL port type for testing: {chosen_port_type}")

            # Select active port based on chosen type
            try:
                if chosen_port_type == 'trunk':
                    # Trunk ports: need LINK_STATE_UP and transceivers
                    selected_port_obj = RandomizationTool.select_random_port(
                        requested_ports_state=NvosConsts.LINK_STATE_UP,
                        interface_type='sw'  # trunk ports
                    ).get_returned_value()
                    logger.info(f"Selected ACTIVE NVL trunk port for speed testing: {selected_port_obj.name}")
                else:  # access
                    # Access ports: need LINK_LOG_STATE_INITIALIZE (with loopboxes)
                    selected_port_obj = RandomizationTool.select_random_port(
                        requested_ports_logical_state=NvosConsts.LINK_LOG_STATE_INITIALIZE,
                        interface_type='acp'  # access ports
                    ).get_returned_value()
                    logger.info(f"Selected ACTIVE NVL access port for speed testing: {selected_port_obj.name}")

                port_name = selected_port_obj.name
                return NvosConst.NVL_SWITCH_TYPE, selected_port_obj, port_name

            except Exception as e:
                logger.error(f"Failed to find active {chosen_port_type} port: {e}")
                # Try the other port type if available
                other_port_types = [pt for pt in available_port_types if pt != chosen_port_type]
                if other_port_types:
                    other_type = other_port_types[0]
                    logger.info(f"Trying fallback to {other_type} ports")
                    try:
                        selected_port_obj = RandomizationTool.select_random_port(
                            requested_ports_state=NvosConsts.LINK_STATE_UP,
                            interface_type=(NvlInterfaceConsts.SW_INTERFACE_TYPE if other_type == NvlInterfaceConsts.TRUNK_PORT_TYPE
                                            else NvlInterfaceConsts.ACP_PORT_TYPE)
                        ).get_returned_value()
                        port_name = selected_port_obj.name
                        logger.info(f"Fallback successful - selected {other_type} port: {port_name}")
                        return NvosConst.NVL_SWITCH_TYPE, selected_port_obj, port_name

                    except Exception as e2:
                        logger.error(f"Fallback to {other_type} ports also failed: {e2}")

                pytest.skip(f"No active NVL ports available for speed testing (tried {available_port_types})")

    else:
        raise Exception("Unable to determine system type - neither interface_list nor nvl_ports_list found")


def _get_current_and_supported_speeds(selected_port, system_type, port_name):
    """Get current speed and supported speeds based on system type."""
    with allure.step(f"Read current speed and supported speeds for port {port_name}"):
        return InterfaceConfigurationTool.get_current_and_supported_speeds(selected_port, system_type, port_name)


def _choose_different_speed(current_speed, supported_speeds, port_name):
    """
    Select a random speed that's different from the current configuration.

    This function filters out the current speed from the list of supported speeds
    and randomly selects one of the remaining options. If no alternative speeds
    are available, it skips the test with an informative message.

    Args:
        current_speed: Current speed configuration (e.g., 'XDR', '100G')
        supported_speeds: List of all supported speeds (e.g., ['XDR', 'hdr', 'fdr'])
        port_name: Interface name for logging (e.g., 'sw2p1')

    Returns:
        str: Randomly selected speed that differs from current_speed

    Example:
        >>> new_speed = _choose_different_speed('XDR', ['XDR', 'hdr', 'fdr'], 'sw2p1')
        >>> print(new_speed)  # Output: 'hdr' or 'fdr' (randomly chosen)

    Raises:
        pytest.skip: If no alternative speeds are available for testing
    """
    available_speeds_other_than_original = [speed.strip() for speed in supported_speeds if speed.strip() != current_speed]
    if not available_speeds_other_than_original:
        pytest.skip(f"No alternative speeds available for port {port_name}. Current: {current_speed}, Supported: {supported_speeds}")

    new_speed = RandomizationTool.select_random_value(available_speeds_other_than_original).get_returned_value()
    logger.info(f"Chosen different speed for {port_name}: {new_speed} (original was: {current_speed})")
    return new_speed


def _test_speed_configuration_cycle(selected_port, original_speed, new_speed, system_type, port_name):
    """
    Execute a comprehensive 3-step speed configuration test cycle.

    This function performs rigorous speed configuration testing by executing three
    consecutive configuration changes, verifying each step to ensure the interface
    responds correctly to speed changes and can reliably switch between speeds.

    The 3-step cycle tests:
    1. Configure new speed → verify it applied correctly
    2. Revert to original speed → verify it reverted correctly
    3. Configure new speed again → verify it applied correctly again

    Args:
        selected_port: Port object representing the interface to test
        original_speed: Original speed value to revert to (e.g., 'XDR', '100G')
        new_speed: New speed value to test with (e.g., 'hdr', '200G')
        system_type: Either 'IB' or 'NVL' (determines parameter names)
        port_name: Interface name for logging (e.g., 'sw2p1')

    Example:
        >>> _test_speed_configuration_cycle(my_port, 'XDR', 'hdr', 'IB', 'sw2p1')
        # Executes: XDR → hdr → XDR → hdr (with verification at each step)
    """
    # Step 1: Configure new speed
    _configure_and_verify_speed(selected_port, new_speed, system_type, port_name, f"Set {_get_speed_param_name(system_type)} '{new_speed}' for port '{port_name}'")

    # Step 2: Configure back to original
    _configure_and_verify_speed(selected_port, original_speed, system_type, port_name, f"Set {_get_speed_param_name(system_type)} back to original '{original_speed}' for port '{port_name}'")

    # Step 3: Configure new speed again
    _configure_and_verify_speed(selected_port, new_speed, system_type, port_name, f"Set {_get_speed_param_name(system_type)} '{new_speed}' again for port '{port_name}'")


def _configure_and_verify_speed(selected_port, speed, system_type, port_name, step_description):
    """Configure and verify a single speed change."""
    InterfaceConfigurationTool.configure_and_verify_speed(selected_port, speed, system_type, port_name, step_description)


def _get_speed_param_name(system_type):
    """Get the speed parameter name based on system type."""
    return InterfaceConfigurationTool.get_speed_param_name(system_type)


def _verify_and_cleanup_speed_after_upgrade(selected_port, original_speed, expected_speed, device):
    """Verify speed configuration is preserved after upgrade and clean up."""
    _verify_speed_preserved_after_upgrade(selected_port, expected_speed, device)
    _unset_speed_configuration(selected_port, device)
    _verify_speed_back_to_original_after_unset(selected_port, original_speed, device)


def _verify_and_cleanup_speed_after_upgrade_nvl_access(selected_port, original_speed, expected_speed, device, nvl_access_info):
    """Verify speed preserved on a sample port, then unset ALL access ports and verify restoration."""
    from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
    from ngts.tests_nvos.interfaces.nvl_port.helpers import validate_ports_state_and_speed
    from retry.api import retry_call

    _verify_speed_preserved_after_upgrade(selected_port, expected_speed, device)

    port_names = nvl_access_info['port_names']
    access_ports_range = nvl_access_info['range_name']
    default_speed = nvl_access_info['default_speed']
    all_ports = Port(access_ports_range)

    with allure.step(f"Unset speed on all access ports: {access_ports_range}"):
        all_ports.interface.link.unset(op_param='speed', apply=True, ask_for_confirmation=True).verify_result()
        logger.info(f"Unset speed on {access_ports_range}")

    with allure.step(f"Verify all access ports restored to {default_speed}"):
        retry_call(validate_ports_state_and_speed, [default_speed, port_names, 'acp'],
                   exceptions=AssertionError, tries=6, delay=30)
        logger.info(f"All access ports restored to {default_speed}")


def _verify_speed_preserved_after_upgrade(selected_port, expected_speed, device):
    """Verify that the speed configuration is preserved after upgrade."""
    InterfaceConfigurationTool.verify_speed_configuration(
        selected_port, expected_speed, device,
        f"Verify speed configuration is preserved after upgrade for port {selected_port.name}")


def _unset_speed_configuration(selected_port, device):
    """Unset speed configuration for cleanup."""
    InterfaceConfigurationTool.unset_speed_configuration(selected_port, device)


def _verify_speed_back_to_original_after_unset(selected_port, original_speed, device):
    """Verify that the speed is back to original after unset."""
    InterfaceConfigurationTool.verify_speed_configuration(
        selected_port, original_speed, device,
        f"Verify speed is back to original after unset for port {selected_port.name}")


def _get_system_type_from_device(device):
    """Get system type from device object."""
    return InterfaceConfigurationTool.get_system_type_from_device(device)


def verify_current_version(original_version, system, device):
    with allure.step(f"Verify that current image is {original_version}"):
        current_version = system.version.get_nvos_image_version()
        assert current_version == original_version, f"Current version is invalid: {current_version}, expected: {original_version}"


def run_post_downgrade_cheks(ib_router):
    """run various optional checks after the machine finish downgrade"""
    with allure.step("Running post downgrade checks if there's any"):
        if ib_router:
            verify_ib_router_post_downgrade()


def verify_ib_router_post_downgrade():
    """run various checks on ib router machine state after downgrade took place"""
    IbRouterTool.verify_leaf_port_mapping(expect_disabled=True)
    IbRouterTool.verify_profile_status(SystemConsts.PROFILE_STATE_DISABLED, 1)


def run_ib_router_pre_upgrades_steps(topology_obj, engines):
    """prepare the ib router setup before upgrade flow take place"""
    IbRouterTool.set_router_setup(topology_obj, engines)


def run_ib_router_post_upgrade_steps(topology_obj, engines):
    """prepare the ib router setup before upgrade flow take place"""
    IbRouterTool.verify_profile_status(expected_profile_status=SystemConsts.PROFILE_STATE_ENABLED, expected_swid_number=4)
    IbRouterTool.verify_leaf_port_mapping(expect_disabled=False)


def run_pre_upgrade_steps(topology_obj, engines, ib_router):
    """run various optional checks before the machine starts the upgrade stage"""
    with allure.step("Running pre upgrade actions if there's any"):
        if ib_router:
            run_ib_router_pre_upgrades_steps(topology_obj, engines)


def run_post_upgrade_cheks(topology_obj, engines, ib_router):
    """run various optional checks after the machine finish downgrade"""
    with allure.step("Running post upgrade checks if there's any"):
        if ib_router:
            run_ib_router_post_upgrade_steps(topology_obj, engines)
