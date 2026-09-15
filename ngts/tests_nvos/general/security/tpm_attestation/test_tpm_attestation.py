import random

import pytest

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests_nvos.general.security.tpm_attestation.helpers import *
from ngts.tools.test_utils import allure_utils as allure


@pytest.mark.security
@pytest.mark.tpm
def test_only_aik_file_at_first(engines):
    """
    Verify that tpm dir contains only AIK file, when no tpm actions done before.

    1. show tpm dir
    2. verify dir contains only AIK file
    """
    verify_only_aik_at_tpm_dir(engines)


@pytest.mark.security
@pytest.mark.tpm
def test_upload_bad_tpm_filename(random_api, engines, remote_engine):
    """
    Verify that upload with bad filename param fails

    1. upload with bad filename param
    2. verify upload fail
    """

    with allure.step('upload with bad filename param'):
        bad_filename = 'zz'
        res = System().security.tpm.action_upload(bad_filename, get_scp_url(remote_engine, bad_filename))
    with allure.step('verify upload failed'):
        res.verify_result(False)


@pytest.mark.security
@pytest.mark.tpm
def test_upload_aik(random_api, engines, remote_engine):
    """
    Verify that upload AIK action works

    1. upload AIK file
    2. sanity check for uploaded file
    """

    with allure.step('upload AIK file'):
        System().security.tpm.action_upload(AIK_FILENAME, get_scp_url(remote_engine, AIK_FILENAME)).verify_result()
    with allure.step('sanity check for uploaded file'):
        sanity_check_uploaded_file(engines, AIK_FILE_PATH, remote_engine.ip, remote_engine.username,
                                   remote_engine.password, f'{REMOTE_PATH}/{AIK_FILENAME}')


@pytest.mark.security
@pytest.mark.tpm
def test_upload_quote_before_generate(random_api, engines, remote_engine):
    """
    Verify that upload quote fails when there is no generated quote

    1. upload quote
    3. verify failure
    """

    with allure.step('upload quote'):
        res = System().security.tpm.action_upload(QUOTE_FILENAME, get_scp_url(remote_engine, QUOTE_FILENAME))
    with allure.step('verify upload fail'):
        res.verify_result(False)


@pytest.mark.security
@pytest.mark.tpm
def test_generate_quote_bad_param(random_api, engines):
    """
    Verify that generate quote with bad param fails and not producing any quote file

    1. attempt generate quote with bad param
    2. verify error
    3. verify no quote file generated
    """

    with allure.step('attempt generate quote with bad param, expect error'):
        tpm = System().security.tpm
        valid_algo = random.choice(list(SUPPORTED_ALGORITHMS))
        invalid_algo = random.choice(list(UNSUPPORTED_ALGORITHMS))
        # empty param
        tpm.action_generate_quote().verify_result(False)
        tpm.action_generate_quote(pcrs=VALID_PCRS_PARAM).verify_result(False)
        tpm.action_generate_quote(pcrs=VALID_PCRS_PARAM, algorithm=valid_algo).verify_result(False)
        tpm.action_generate_quote(nonce=VALID_NONCE_PARAM).verify_result(False)
        tpm.action_generate_quote(nonce=VALID_NONCE_PARAM, algorithm=valid_algo).verify_result(False)
        # bad param
        bad = 'zz'
        tpm.action_generate_quote(pcrs=bad, nonce=VALID_NONCE_PARAM).verify_result(False)
        tpm.action_generate_quote(pcrs=bad, nonce=VALID_NONCE_PARAM, algorithm=valid_algo).verify_result(False)
        tpm.action_generate_quote(pcrs=VALID_PCRS_PARAM, nonce=bad).verify_result(False)
        tpm.action_generate_quote(pcrs=VALID_PCRS_PARAM, nonce=bad, algorithm=valid_algo).verify_result(False)
        tpm.action_generate_quote(pcrs=VALID_PCRS_PARAM, nonce=VALID_NONCE_PARAM, algorithm=bad).verify_result(False)
        tpm.action_generate_quote(pcrs=VALID_PCRS_PARAM, nonce=VALID_NONCE_PARAM, algorithm=invalid_algo).verify_result(False)
        # too large nonce
        long_str = ''.join(['a' for _ in range(MAX_CHARS_NONCE + 1)])
        tpm.action_generate_quote(pcrs=VALID_PCRS_PARAM, nonce=long_str).verify_result(False)
        tpm.action_generate_quote(pcrs=VALID_PCRS_PARAM, nonce=long_str, algorithm=valid_algo).verify_result(False)
    with allure.step('verify no quote file generated'):
        verify_only_aik_at_tpm_dir(engines)


@pytest.mark.nvos_ci
@pytest.mark.security
@pytest.mark.tpm
def test_generate_quote(random_api, engines, devices):
    """
    Verify that generate quote works

    1. generate quote
    2. verify success
    3. verify quote file generated
    4. sanity check for generated file
    """

    with allure.step('generate quote'):
        tpm = System().security.tpm
        tpm.action_generate_quote(VALID_PCRS_PARAM, VALID_NONCE_PARAM).verify_result()
        for valid_algo in list(SUPPORTED_ALGORITHMS):
            tpm.action_generate_quote(VALID_PCRS_PARAM, VALID_NONCE_PARAM, algorithm=valid_algo).verify_result()
    with allure.step('verify quote file generated'):
        tpm_files = TpmTool(engines.dut).get_files_in_tpm_dir()
        assert QUOTE_FILENAME in tpm_files, \
            f'''quote file "{QUOTE_FILENAME}" was not generated
            tpm dir content: {tpm_files}'''
    with allure.step('sanity check for generated file'):
        sanity_check_generated_quote(engines, VALID_NONCE_PARAM)


@pytest.mark.security
@pytest.mark.tpm
def test_upload_quote(random_api, engines, remote_engine):
    """
    Verify that upload quote works

    1. generate quote
    2. upload quote
    3. sanity check for uploaded file
    """

    with allure.step('generate quote'):
        tpm = System().security.tpm
        tpm.action_generate_quote(VALID_PCRS_PARAM, VALID_NONCE_PARAM).verify_result()
    with allure.step('upload quote'):
        tpm.action_upload(QUOTE_FILENAME, get_scp_url(remote_engine, QUOTE_FILENAME)).verify_result()
    with allure.step('sanity check for uploaded file'):
        sanity_check_uploaded_file(engines, QUOTE_FILE_PATH, remote_engine.ip, remote_engine.username,
                                   remote_engine.password, f'{REMOTE_PATH}/{QUOTE_FILENAME}')


@pytest.mark.security
@pytest.mark.tpm
def test_generate_quote_overrides_file(engines):
    """
    Verify generate quote overrides an existing quote file

    1. generate dummy file as quote file
    2. generate quote again
    3. verify the original quote file was overridden
    """
    with allure.step('generate dummy file as "original quote"'):
        dummy_quote_content = 'abcalonxyz'
        engines.dut.run_cmd(f'echo {dummy_quote_content} > /tmp/{QUOTE_FILENAME}')
        engines.dut.run_cmd(f'sudo mv /tmp/{QUOTE_FILENAME} {QUOTE_FILE_PATH}')
    with allure.step('generate quote again'):
        System().security.tpm.action_generate_quote(VALID_PCRS_PARAM, VALID_NONCE_PARAM).verify_result()
    with allure.step('verify the original quote file was overridden'):
        cur_quote_content = TpmTool(engines.dut).get_quote_file_content()
        assert cur_quote_content != dummy_quote_content, \
            f'''quote file content same as original dummy quote (probably was not overridden)
            content: {dummy_quote_content}'''


@pytest.mark.dut_reboot
@pytest.mark.security
@pytest.mark.tpm
def test_tpm_reboot_cases(engines, devices, save_local_timezone):
    """
    Verify that:
        1. AIK file is re-generated in every boot
        2. reboot keeps quote file

     1. before reboot
         1. get AIK file creation time
         2. generate quote
     2. reboot
     3. checks after reboot
        1. verify boot generated new AIK file
            1. get new AIK file creation time
            2. verify creation times are different
        2. verify reboot keeps quote file
    """
    with allure.step('steps before reboot'):
        with allure.step('get current AIK file creation time'):
            aik_time1 = get_file_creation_time(engines.dut, AIK_FILE_PATH)
        with allure.step('generate quote'):
            system = System()
            system.security.tpm.action_generate_quote(VALID_PCRS_PARAM, VALID_NONCE_PARAM).verify_result()
            quote_time1 = get_file_creation_time(engines.dut, QUOTE_FILE_PATH)
    with allure.step('reboot'):
        system.action_reboot('force').verify_result()
    with allure.step('checks after reboot'):
        with allure.step('verify boot generated new AIK file'):
            with allure.step('get new AIK file creation time'):
                aik_time2 = get_file_creation_time(engines.dut, AIK_FILE_PATH)
            with allure.step('verify creation times are different'):
                assert aik_time1 != aik_time2, \
                    f'''creation times are equal but expected to be different.
                    before reboot: {aik_time1}
                    after reboot: {aik_time2}'''
        with allure.step('verify quote file kept after reboot'):
            quote_time2 = get_file_creation_time(engines.dut, QUOTE_FILE_PATH)
            assert quote_time1 == quote_time2, \
                f'''creation times are different but expected to be equal.
                before reboot: {quote_time1}
                after reboot: {quote_time2}'''


@pytest.mark.security
@pytest.mark.tpm
def test_tpm_oiak(engines, devices, save_local_timezone):
    """
    Verify oIAK functionality

     1. Check oIAK show by default on switch, should not exist
     2. Import oIAK with incorrect public key and check it not applied
     3. Import oIAK with correct public key and verify all fields in output
     4. Generate quote and check oIAK certificate in
     5. Remove oIAK certificate and check it removed
    """
    system = System()
    dut = engines.dut
    valid_algo = random.choice(list(SUPPORTED_ALGORITHMS))

    with allure.step('Check oIAK by default, should not exist on switch'):
        show_output = system.security.tpm.oaik.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        assert output_dictionary == {}

    with allure.step('Import oIAK with incorrect public key'):
        mismatch_public_key_cert = get_oiak_cert(engines.sonic_mgmt, OIAK_DUMMY_CERT_PATH)
        response = system.security.tpm.oaik.action_import_tpm_oiak(dut_engine=dut, data=f'{mismatch_public_key_cert}').ignore_result()
        assert 'Error: Public key mismatch' in response.info, "Switch accept certificate with mismatched public key"

        with allure.step('Check oIAK with mismatched public key not accept'):
            show_output = system.security.tpm.oaik.show()
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
            assert output_dictionary == {}

    with allure.step('Import correct oIAK certificate'):
        system.security.tpm.oaik.action_import_tpm_oiak(
            dut_engine=dut, remote_url=f'scp://{dut.username}:{dut.password}@{dut.ip}{AIK_FILE_PATH}')

        with allure.step('Verify fields in oIAK certificate'):
            show_output = system.security.tpm.oaik.show()
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
            assert all(item in output_dictionary['plain'] for item in
                       ["Certificate", "Data", "Serial Number", "Signature Algorithm",
                        "Issuer"]), "Required items not found in output"

    with allure.step(f'Generate quote with algo:{valid_algo} pcrs:{VALID_PCRS_PARAM} nonce:{VALID_NONCE_PARAM}'):
        system.security.tpm.action_generate_quote(pcrs=VALID_PCRS_PARAM, nonce=VALID_NONCE_PARAM, algorithm=valid_algo).verify_result()
        with allure.step('verify quote file generated'):
            tpm_files = TpmTool(dut).get_files_in_tpm_dir()
            assert QUOTE_FILENAME in tpm_files, \
                f'''quote file "{QUOTE_FILENAME}" was not generated
                tpm dir content: {tpm_files}'''
        with allure.step('sanity check for generated file'):
            sanity_check_generated_quote(engines, VALID_NONCE_PARAM)
        with allure.step('sanity check for generated file'):
            quote_content = TpmTool(engines.dut).get_quote_file_content()
            assert 'oIAK' in quote_content, f'OIAK cert not exist in quote {quote_content}'

    with allure.step('Remove oIAK certificate and check it not exist in output'):
        dut.run_cmd(f"sudo rm {OAIK_FILE_PATH}")
        show_output = system.security.tpm.oaik.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        assert output_dictionary == {}, f'OIAK output {output_dictionary} is not empty'
