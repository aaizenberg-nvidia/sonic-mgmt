import datetime

import pytest

from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure


cumulus_owner = "yleibovichwe"


@pytest.fixture(autouse=True)
def rotate_logs():
    """
    Rotate logs before running the test
    """
    system = System(None)
    system.log.rotate_logs()


@pytest.mark.system
@pytest.mark.tech_support
@pytest.mark.cumulus
@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_techsupport_show_and_since(engines, test_name, random_api, devices, serial_log_analyzers):
    """
    Combined test for show format validation, folder name validation, and since functionality.
    Uses single generation since test_techsupport_file_operations validates file accumulation with 4 files.

    Test flow:
        1. Test invalid date handling (no generation)
        2. Generate tech-support with 'since' parameter
        3. Validate the file was created with since functionality
        4. Validate folder name format (nvos_dump_<hostname>_<date>_<time>.tar.gz)
        5. Validate show output format (latest key, paths)
        6. Cleanup
    """

    serial_analyzer = list(serial_log_analyzers.values())[0]
    system = System(None)
    operation = devices.dut.generate_tech_support
    path = devices.dut.techsupport_files_path
    file1 = None
    folder = None

    with allure.step('Test techsupport show and since functionality'):
        # --- INVALID DATE HANDLING (no generation) ---
        with allure.independent_step('Invalid date handling'):
            invalid_date_syntax = '20206610'
            with allure.step(f'Validate generate fails with invalid date {invalid_date_syntax}'):
                output, duration = system.techsupport.action_generate(option=SystemConsts.ACTIONS_GENERATE_SINCE,
                                                                      since_time=invalid_date_syntax)
                assert any(msg in output for msg in ['Action failed with the following', 'invalid date']), \
                    f"Expected error message for invalid date, got: {output}"

            invalid_date_syntax = 'aabbccdd'
            with allure.step(f'Validate generate fails with invalid date {invalid_date_syntax}'):
                output, duration = system.techsupport.action_generate(option=SystemConsts.ACTIONS_GENERATE_SINCE,
                                                                      since_time=invalid_date_syntax)
                assert any(msg in output for msg in ['Action failed with the following', 'invalid date']), \
                    f"Expected error message for invalid date, got: {output}"

        # --- GENERATE WITH SINCE PARAMETER ---
        with allure.independent_step('Generate tech-support with since parameter'):
            output_dictionary_before = list(Tools.OutputParsingTool.parse_show_files_to_dict(
                system.techsupport.files.show()).get_returned_value().values())

            yesterday = datetime.datetime.today() - datetime.timedelta(days=1)
            yesterday_str = yesterday.strftime("%Y%m%d")

            with serial_analyzer.stage("Generate tech-support with since"):
                folder, duration = system.techsupport.action_generate(
                    engines.dut, SystemConsts.ACTIONS_GENERATE_SINCE, yesterday_str, test_name=test_name)

            OperationTime.verify_operation_time(duration, operation, devices).verify_result()
            file1 = system.techsupport.file_name

        # --- SINCE FUNCTIONALITY VALIDATION ---
        with allure.independent_step('Validate since functionality'):
            output_dictionary_after = list(Tools.OutputParsingTool.parse_show_files_to_dict(
                system.techsupport.files.show()).get_returned_value().values())
            assert folder in output_dictionary_after, \
                f"Tech-support folder {folder} not found in show output after generation with since"
            validate_techsupport_output(output_dictionary_before, output_dictionary_after, 1)

        # --- FOLDER NAME VALIDATION ---
        with allure.independent_step('Validate folder name format'):
            system_output = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
            hostname = system_output[SystemConsts.HOSTNAME]
            assert 'nvos_dump_' + hostname in folder, \
                f'Tech-support name should contain nvos_dump_{hostname}, got: {folder}'

        # --- SHOW FORMAT VALIDATION ---
        with allure.independent_step('Validate show command format'):
            show_output = system.techsupport.files.show()
            output_dict = Tools.OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
            assert SystemConsts.LATEST_KEY in output_dict, \
                f"Output of show tech-support is missing key '{SystemConsts.LATEST_KEY}'. Existing keys: {output_dict.keys()}"
            latest_file = output_dict.pop(SystemConsts.LATEST_KEY)[SystemConsts.PATH_KEY]
            output_dict = {key: value[SystemConsts.PATH_KEY] for key, value in output_dict.items()}
            assert latest_file == find_latest_key(output_dict), (
                f"Output of show tech-support contains a file marked 'latest', but that file either doesn't exist or is not"
                f" really the latest file. File is {latest_file}."
            )
            assert list(output_dict.keys()) == [full_path.replace(path, '')
                                                for full_path in output_dict.values()], \
                f"Output of show tech-support has mismatch between keys (file names) and full-paths: {output_dict.items()}"

        # --- SIZE VALIDATION ---
        with allure.independent_step('Validate tech-support size'):
            system.techsupport.verify_size(engines.dut, folder)

        # --- CLEANUP ---
        with allure.independent_step('Cleanup'):
            if file1:
                system.techsupport.files.file_name[file1].action_delete()


@pytest.mark.system
@pytest.mark.tech_support
@pytest.mark.cumulus
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_techsupport_file_operations(engines, test_name, random_api, devices, serial_log_analyzers):
    """
    Combined test for multiple file operations: generate multiple files, upload, and delete.
    Generates 4 files, validates file accumulation/latest tracking, tests upload (scp+sftp),
    and tests delete operations.

    Test flow:
        1. Generate 4 tech-support files, validating show output after each
        2. Test upload functionality using one of the generated files:
           - Upload non-existent file (error handling)
           - Upload via scp protocol
           - Upload via sftp protocol
           - Test invalid URL error handling
        3. Test delete functionality:
           - Delete first file, verify success
           - Verify first file gone, others remain
           - Try delete already-deleted file, verify error
        4. Cleanup remaining files
    """
    serial_analyzer = list(serial_log_analyzers.values())[0]
    system = System(None)
    operation = devices.dut.generate_tech_support
    path = devices.dut.techsupport_files_path
    delete_success_message = devices.dut.techsupport_delete_success_message
    upload_success_message = devices.dut.techsupport_upload_success_message
    files_names = []
    files_folders = []

    with allure.step('Test techsupport file operations'):
        # --- MULTIPLE GENERATIONS PART ---
        with allure.step('Generate 4 tech-support files'):
            output_dictionary_before = list(Tools.OutputParsingTool.parse_show_files_to_dict(
                system.techsupport.files.show()).get_returned_value().values())
            for i in range(0, 4):
                with allure.independent_step(f"Generate Tech-Support {i + 1}"):
                    with serial_analyzer.stage(f"Generate tech-support {i + 1}"):
                        folder, duration = system.techsupport.action_generate(test_name=test_name)
                    files_names.append(system.techsupport.file_name)
                    files_folders.append(folder)

                    with allure.step("Validate output"):
                        output_dictionary_after = list(Tools.OutputParsingTool.parse_show_files_to_dict(
                            system.techsupport.files.show()).get_returned_value().values())
                        # Validate based on actual number of files generated so far
                        validate_techsupport_output(output_dictionary_before, output_dictionary_after, len(files_names))

                    with allure.step('Validate show tech-support command format'):
                        show_output = system.techsupport.files.show()
                        files_dict = Tools.OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
                        assert SystemConsts.LATEST_KEY in files_dict, \
                            f"Output of show tech-support is missing key '{SystemConsts.LATEST_KEY}'. Existing keys: {files_dict.keys()}"
                        latest_file = files_dict.pop(SystemConsts.LATEST_KEY)[SystemConsts.PATH_KEY]
                        files_dict = {key: value[SystemConsts.PATH_KEY] for key, value in files_dict.items()}
                        assert latest_file == find_latest_key(files_dict), (
                            f"Output of show tech-support contains a file marked 'latest', but that file either doesn't exist or is not"
                            f" really the latest file. File is {latest_file}."
                        )
                        assert list(files_dict.keys()) == [full_path.replace(SystemConsts.TECHSUPPORT_FILES_PATH, '')
                                                           for full_path in files_dict.values()], \
                            f"Output of show tech-support has mismatch between keys (file names) and full-paths: {files_dict.items()}"

                    with allure.step('Validate operation time for tech-support generation'):
                        OperationTime.verify_operation_time(duration, operation, devices).verify_result()

        # --- UPLOAD VALIDATION PART (using generated files) ---
        with allure.independent_step('Upload functionality'):
            # Use the last generated file for upload tests (if any were generated)
            upload_file = files_names[-1].replace(path, '') if files_names else None
            assert upload_file, "No tech-support files were generated, cannot test upload functionality"

            # Test upload with scp
            protocol = 'scp'
            invalid_url_1, invalid_url_2, upload_path, target_engine = _create_upload_urls(engines, devices, protocol)

            with allure.step('Upload non-existent file error handling'):
                system.techsupport.files.file_name['nonexist'].action_upload(upload_path=upload_path).verify_result(
                    False, expected_value="File not found: nonexist")

            with allure.step('Upload via scp protocol'):
                system.techsupport.files.file_name[upload_file].action_upload(upload_path).verify_result(
                    expected_value=upload_success_message)

                with allure.step('Verify the uploaded file exists in target path'):
                    output = target_engine.run_cmd('ls /tmp/')
                    assert upload_file in output

                # Cleanup uploaded file from target
                target_engine.run_cmd(f'rm -f /tmp/{upload_file}')

            # Test upload with sftp (reusing the same generated file)
            protocol = 'sftp'
            _, _, upload_path_sftp, _ = _create_upload_urls(engines, devices, protocol)

            with allure.step('Upload via sftp protocol'):
                system.techsupport.files.file_name[upload_file].action_upload(upload_path_sftp).verify_result(
                    expected_value=upload_success_message)

                with allure.step('Verify the uploaded file exists in target path'):
                    output = target_engine.run_cmd('ls /tmp/')
                    assert upload_file in output

                # Cleanup uploaded file from target
                target_engine.run_cmd(f'rm -f /tmp/{upload_file}')

            with allure.step('Invalid URL error handling'):
                with allure.step('Upload to invalid url - url not in right format'):
                    system.techsupport.files.file_name['nonexist'].action_upload(upload_path=invalid_url_1).verify_result(
                        False, expected_value=devices.dut.techsupport_file_not_found_message)

                with allure.step('Upload to invalid url - non supported transfer protocol'):
                    system.techsupport.files.file_name['nonexist'].action_upload(upload_path=invalid_url_2).verify_result(
                        False, expected_value=devices.dut.techsupport_file_not_found_message)

        # --- DELETE VALIDATION PART ---
        with allure.independent_step('Delete functionality'):
            assert len(files_names) >= 2, f"Need at least 2 files for delete testing, got {len(files_names)}"
            first_file = files_names[0]
            first_folder = files_folders[0]
            second_folder = files_folders[1]

            with allure.step('Delete first file and verify'):
                system.techsupport.files.file_name[first_file.replace(path, '')].action_delete().verify_result(
                    expected_value=delete_success_message)

                output_dictionary_after_delete = list(Tools.OutputParsingTool.parse_show_files_to_dict(
                    system.techsupport.files.show()).get_returned_value().values())

                with allure.step('Verify {} deleted and {} still exists'.format(first_folder, second_folder)):
                    assert first_folder not in output_dictionary_after_delete, \
                        "{} still exists after deleting it".format(first_folder)
                    assert second_folder in output_dictionary_after_delete, \
                        "{} does not exist".format(second_folder)

            with allure.step('Delete non-existent file error handling'):
                system.techsupport.files.file_name[first_file.replace(path, '')].action_delete().verify_result(
                    should_succeed=False, expected_value="File not found")

            # Mark first file as deleted for cleanup
            files_names[0] = None

        # --- CLEANUP ---
        with allure.independent_step('Cleanup remaining files'):
            for file_name in files_names:
                if file_name:  # Skip already deleted files
                    system.techsupport.files.file_name[file_name].action_delete()


def validate_techsupport_output(output_dictionary_before, output_dictionary_after, number_of_expected_files):
    """
    Asserts that our actions caused the correct number of files to be created.
    :param output_dictionary_before: Output of the `nv show tech-support` command.
    :param output_dictionary_after: Output of the same command after some actions were taken.
    :param number_of_expected_files: The number of dump files that we expect to be created after the actions.
    """
    with allure.step('Validating the generate command and show command working as expected'):
        assert len(set(output_dictionary_after) - set(output_dictionary_before)) == number_of_expected_files, \
            "at least one of the new tech-support folders not found"


def find_latest_key(tech_support_dict):
    """
    Find the key in the dictionary with the latest timestamp based on
    the `_YYYYMMDD_HHMMSS` format present in the key strings.

    Example:
        input_dict = {
            "nvos_dump_mtvr-croc-19-mgmt2_20241118_203558.tar.gz": {...},
            "nvos_dump_mtvr-croc-19-mgmt2_20241119_001126.tar.gz": {...},
            "nvos_dump_mtvr-croc-19_20241118_232312.tar.gz": {...}
        }

        result = find_latest_key(tech_support_dict)
        # result -> "/host/dump/nvos_dump_mtvr-croc-19-mgmt2_20241119_001126.tar.gz"
    """
    return TestToolkit.devices.dut.techsupport_files_path + max(tech_support_dict.keys(), key=lambda x: x.split('_')[-2:])


def _create_upload_urls(engines, devices, protocol):
    """
    Helper function to create upload URLs based on device type.

    Args:
        engines: The engines object containing device connections
        devices: The devices object containing device information
        protocol: The transfer protocol to use (scp or sftp)

    Returns:
        tuple: (invalid_url_1, invalid_url_2, upload_path, target_engine)
    """
    if devices.dut.is_ib() or devices.dut.is_nvl():
        # For IB devices, use player (sonic_mgmt)
        player = engines['sonic_mgmt']
        invalid_url_1 = '{}://{}:{}{}/tmp/'.format(protocol, player.username, player.password, player.ip)
        invalid_url_2 = 'ffff://{}:{}@{}/tmp/'.format(player.username, player.password, player.ip)
        upload_path = '{}://{}:{}@{}/tmp/'.format(protocol, player.username, player.password, player.ip)
        target_engine = player
    else:
        # For ETH devices, use engines.dut
        invalid_url_1 = "\'{}://{}:{}{}:/tmp/\'".format(protocol, engines.dut.username, engines.dut.password, engines.dut.ip)
        invalid_url_2 = "\'ffff://{}:{}@{}:/tmp/\'".format(engines.dut.username, engines.dut.password, engines.dut.ip)
        upload_path = "\'{}://{}:{}@{}:/tmp/\'".format(protocol, engines.dut.username, engines.dut.password, engines.dut.ip)
        target_engine = engines.dut

    return invalid_url_1, invalid_url_2, upload_path, target_engine
