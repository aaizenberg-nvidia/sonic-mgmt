import json
import logging
import os
import random
import traceback
from typing import List, Tuple

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from ngts.nvos_constants.constants_nvos import TestFlowType
from ngts.nvos_tools.infra.CmdRunner import CmdRunner
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.TpmTool import TpmTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.Spdm import SpdmComponent, SpdmComponentFields
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import ADMIN
from ngts.tests_nvos.general.security.bmc.bmc_erot_attestation.client_scripts.verify_cert_chain.verify_cert_chain import (
    order_certs,
    parse_certs_str,
    save_ordered_chain,
    validate_certs_chain,
)
from ngts.tests_nvos.general.security.bmc.bmc_erot_attestation.client_scripts.verify_spdm_measurements.verify_spdm_measurements import (
    verify_spdm_measurements,
)
from ngts.tests_nvos.general.security.bmc.bmc_erot_attestation.constants import NOT_EMPTY, VALID_NONCE_LEN, SpdmConsts
from ngts.tools.test_utils import allure_utils as allure


def get_component_obj(component_name: str, supported_components: List[str]) -> SpdmComponent:
    """
    Get SPDM component object by component name.

    Uses spdm.get_component() for device-agnostic access, which creates the
    component with the correct path based on the actual component name
    (e.g., 'ERoT_NVSwitch_0' for Juliet, 'IRoT_NVSwitch_0' for Rosalind).
    """
    assert component_name in supported_components, f'given component name "{component_name}" not in {supported_components}'
    spdm = System().security.spdm
    return spdm.get_component(component_name)


def randomize_hex_str(length: int = VALID_NONCE_LEN) -> str:
    random_valid_nonce = ''.join(random.choice('0123456789ABCDEFabcdef') for _ in range(length))
    logging.info(f'randomized nonce: random_valid_nonce')
    return random_valid_nonce


def _strip_odata_keys(data: dict) -> dict:
    """Drop Redfish/OData metadata keys (e.g. @odata.type) before NV-BMC compares."""
    return {k: v for k, v in data.items() if '@odata' not in k}


def randomize_non_hex_str(length: int = VALID_NONCE_LEN) -> str:
    idx_for_non_hex_char = random.randint(0, length)
    prefix = ''.join(random.choice('0123456789ABCDEFabcdef') for _ in range(idx_for_non_hex_char))
    suffix = ''.join(random.choice('0123456789ABCDEFabcdef') for _ in range(length - idx_for_non_hex_char - 1))
    non_hex_char = random.choice('hijklmnopqrstuvwxyzHIJKLMNOPQRSTUVWXYZ')
    rand_non_hex_nonce = prefix + non_hex_char + suffix
    logging.info(f'randomized non hex str:\n\t"{prefix}" "{non_hex_char}" "{suffix}"\n\t"{rand_non_hex_nonce}"')
    return rand_non_hex_nonce


def verify_component_outputs(component_name: str, component_obj: SpdmComponent, component_is_available: bool,
                             expect_cert=None,
                             expect_measurements=None) -> Tuple[dict, dict]:
    if expect_cert:
        expect_cert = clean_cert_str(expect_cert)
    with allure.step('show component'):
        comp_out = OutputParsingTool.parse_json_str_to_dictionary(component_obj.show()).get_returned_value()
    with allure.step(f'verify fields {SpdmConsts.Component.fields} exist'):
        ValidationTool.verify_field_exist_in_json_output(comp_out, SpdmConsts.Component.fields).verify_result()
    with allure.step('show component certificate'):
        cert_out = OutputParsingTool.parse_json_str_to_dictionary(
            component_obj.certificates.show()).get_returned_value()
        cert_out[SpdmConsts.Component.Certificates.CERT_STRING] = clean_cert_str(
            cert_out[SpdmConsts.Component.Certificates.CERT_STRING])
    with allure.step(f'verify fields {SpdmConsts.Component.Certificates.fields} exist'):
        ValidationTool.verify_field_exist_in_json_output(cert_out,
                                                         SpdmConsts.Component.Certificates.fields).verify_result()
    with allure.step('show component measurements'):
        measurements_out = OutputParsingTool.parse_json_str_to_dictionary(
            component_obj.measurements.show()).get_returned_value()
    with allure.step(f'verify fields {SpdmConsts.Component.Measurements.fields} exist'):
        ValidationTool.verify_field_exist_in_json_output(measurements_out,
                                                         SpdmConsts.Component.Measurements.fields).verify_result()
    if component_is_available:
        with allure.step('check values'):
            verify_component_values(component_name, expect_cert, expect_measurements, cert_out, measurements_out)
    else:
        with allure.step('check component has NA values'):
            verify_component_values_na(component_name, cert_out, measurements_out)

    if component_is_available:
        with allure.step('sanity check on values'):
            verify_cert_data_same_as_directly_from_bmc(component_name, cert_out)
            verify_measurements_data_same_as_directly_from_bmc(component_name, measurements_out)

    return cert_out, measurements_out


def verify_component_values_na(component_name: str, cert_out, measurements_out):
    with allure.step(f'verify component "{component_name}" values are NA'):
        with allure.independent_step('check certificate fields'):
            for field, expected_value in SpdmConsts.Component.Certificates.na_values.items():
                with allure.independent_step(f'verify field "{field}" = {expected_value}'):
                    assert cert_out[
                        field] == expected_value, f'certificate field "{field}" not as expected.\nexpected: {expected_value}\nactual: {cert_out[field]}'
        with allure.independent_step('check measurements fields'):
            for field, expected_value in SpdmConsts.Component.Measurements.na_values.items():
                with allure.independent_step(f'verify field "{field}" = {expected_value}'):
                    assert measurements_out[
                        field] == expected_value, f'measurements field "{field}" not as expected.\nexpected: {expected_value}\nactual: {measurements_out[field]}'


def verify_component_values(component_name: str, expect_cert, expect_measurements, cert_out, measurements_out):
    with ((allure.step(f'check component "{component_name}: values'))):
        if expect_cert:
            with allure.independent_step('check certificate value'):
                compare_cert_chains_except_for_leaf(expect_cert, cert_out[SpdmConsts.Component.Certificates.CERT_STRING])
                # assert cert_out[SpdmConsts.Component.Certificates.CERT_STRING] == expect_cert, (
                #     f'certificate field "{SpdmConsts.Component.Certificates.CERT_STRING}" is not as expected.\n'
                #     f'expected: {expect_cert}\nactual: {cert_out[SpdmConsts.Component.Certificates.CERT_STRING]}')
        else:
            with allure.independent_step('check certificate is not empty'):
                assert cert_out[
                    SpdmConsts.Component.Certificates.CERT_STRING] != '', f'certificate field "{SpdmConsts.Component.Certificates.CERT_STRING}" is unexpectedly empty'

        if expect_measurements:
            if expect_measurements is NOT_EMPTY:
                with allure.independent_step('check measurements value is not empty'):
                    assert measurements_out[
                        SpdmConsts.Component.Measurements.SIGNED_MEASUREMENTS] != '', f'measurements field "{SpdmConsts.Component.Measurements.SIGNED_MEASUREMENTS}" is unexpectedly empty'
            else:
                with allure.independent_step('check measurements value'):
                    assert measurements_out[
                        SpdmConsts.Component.Measurements.SIGNED_MEASUREMENTS] == expect_measurements, (
                        f'measurements field "{SpdmConsts.Component.Measurements.SIGNED_MEASUREMENTS}" is not as expected.\n'
                        f'expected: {expect_measurements}\n'
                        f'actual: {measurements_out[SpdmConsts.Component.Measurements.SIGNED_MEASUREMENTS]}')
        else:
            with allure.independent_step('verify measurements initial values'):
                initial_values = SpdmConsts.Component.Measurements.get_initial_values(component_name)
                for field, expected_value in initial_values.items():
                    if expected_value == NOT_EMPTY:
                        with allure.independent_step(f'verify field "{field}" is not empty'):
                            assert measurements_out[
                                field] != '', f'measurements field "{field}" is unexpectedly empty'
                    else:
                        with allure.independent_step(f'verify field "{field}" = {expected_value}'):
                            assert measurements_out[
                                field] == expected_value, f'measurements field "{field}" not as expected.\nexpected: {expected_value}\nactual: {measurements_out[field]}'


def verify_cert_data_same_as_directly_from_bmc(erot_name: str, nv_cert: dict):
    dut_engine: LinuxSshEngine = TestToolkit.engines.dut
    with allure.step('compare certificates data from nv show to data received directly from BMC'):
        with allure.step('get nvos password to bmc from tpm'):
            tpm = TpmTool(dut_engine)
            bmc_password = tpm.get_bmc_admin_password_from_tpm()
        with allure.step('get certificates data directly from bmc'):
            output_file = '/tmp/bmc-certs'
            redfish_erot_name = f'MGX_{erot_name}'
            dut_engine.run_cmd(
                f'curl -k -u {ADMIN}:{bmc_password} -H "Content-Type: application/json" -X GET https://10.0.1.1/redfish/v1/Chassis/{redfish_erot_name}/Certificates/CertChain > {output_file}')
            dut_engine.run_cmd(f'echo "" >> {output_file}')
            bmc_cert_file_content = dut_engine.run_cmd(f'cat {output_file}')
            bmc_cert = OutputParsingTool.parse_json_str_to_dictionary(bmc_cert_file_content).get_returned_value()
            bmc_cert = _strip_odata_keys(bmc_cert)
            dut_engine.run_cmd(f'rm -f {output_file}')
            bmc_cert[SpdmConsts.Component.Certificates.CERT_STRING] = clean_cert_str(
                bmc_cert[SpdmConsts.Component.Certificates.CERT_STRING])
            nv_cert[SpdmConsts.Component.Certificates.CERT_STRING] = clean_cert_str(
                nv_cert[SpdmConsts.Component.Certificates.CERT_STRING])
        with allure.step('compare the data of nv and bmc'):
            ValidationTool.compare_dictionaries(
                _strip_odata_keys(nv_cert), bmc_cert).verify_result()


def clean_cert_str(cert_str: str) -> str:
    logging.info('cleaning certificate string')
    cleaned_cert_str = cert_str.replace('\n', '').replace('\\n', '').strip()

    # Split the string into individual certificates
    begin_cert, end_cert = '-----BEGIN CERTIFICATE-----', '-----END CERTIFICATE-----'
    certs = cleaned_cert_str.split(end_cert)

    formatted_certs = []
    for cert in certs:
        if cert.strip():  # Skip empty strings
            # Remove header if present
            cert_content = cert.replace(begin_cert, '').replace(' ', '')

            # Insert newlines every 64 characters
            formatted_content = '\n'.join([cert_content[i:i + 64] for i in range(0, len(cert_content), 64)])

            # Add header and footer
            formatted_cert = f"{begin_cert}\n{formatted_content}\n{end_cert}"
            formatted_certs.append(formatted_cert)

    # Join all formatted certificates
    res = '\n'.join(formatted_certs)

    logging.debug(f'cleaned cert:\n{res}')
    return res


def verify_measurements_data_same_as_directly_from_bmc(erot_name: str, nv_measurements: dict):
    dut_engine: LinuxSshEngine = TestToolkit.engines.dut
    with allure.step('compare measurements data from nv show to data received directly from BMC'):
        with allure.step('get nvos password to bmc from tpm'):
            tpm = TpmTool(dut_engine)
            bmc_password = tpm.get_bmc_admin_password_from_tpm()
        with allure.step('get measurements data directly from bmc'):
            output_file = '/tmp/bmc-measurements'
            redfish_erot_name = f'MGX_{erot_name}'
            dut_engine.run_cmd(
                f'curl -k -u {ADMIN}:{bmc_password} -H "Content-Type: application/json" -X GET https://10.0.1.1/redfish/v1/ComponentIntegrity/{redfish_erot_name}/Actions/ComponentIntegrity.SPDMGetSignedMeasurements/data > {output_file}')
            dut_engine.run_cmd(f'echo "" >> {output_file}')
            bmc_measurements_file_content = dut_engine.run_cmd(f'cat {output_file}')
            bmc_measurements = OutputParsingTool.parse_json_str_to_dictionary(
                bmc_measurements_file_content).get_returned_value()
            bmc_measurements = _strip_odata_keys(bmc_measurements)
            nv_measurements = _strip_odata_keys(nv_measurements)
            dut_engine.run_cmd(f'rm -f {output_file}')
        with allure.step('compare the data of nv and bmc'):
            ValidationTool.compare_dictionaries(nv_measurements, bmc_measurements).verify_result()


def run_client_measurements_verification_usecanse(component_obj: SpdmComponent, nonce_used_for_last_generate: str,
                                                  component_show_data: dict = None,
                                                  test_flow: str = TestFlowType.GOOD_FLOW):
    test_is_good_flow = test_flow == TestFlowType.GOOD_FLOW
    with allure.step('get component data'):
        component_data = component_show_data or OutputParsingTool.parse_json_str_to_dictionary(
            component_obj.show()).get_returned_value()
        component_data[SpdmComponentFields.CERTIFICATES][
            SpdmConsts.Component.Certificates.CERT_STRING] = clean_cert_str(
            component_data[SpdmComponentFields.CERTIFICATES][SpdmConsts.Component.Certificates.CERT_STRING])
    with allure.step('show cert chain'):
        cert_chain_str = component_data[SpdmComponentFields.CERTIFICATES][SpdmConsts.Component.Certificates.CERT_STRING]
        logging.info(f'certificate chain str:\n{cert_chain_str}')
    with allure.step('show measurements'):
        measurements_data = component_data[SpdmComponentFields.MEASUREMENTS]
        logging.info(f'measurements data:\n{measurements_data}')
    with allure.step('verify'):
        run_client_verification(cert_chain_str, measurements_data, nonce_used_for_last_generate, test_is_good_flow)


def run_client_verification(cert_chain_content: str, measurements_data: dict, nonce: str, expect_success: bool):
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    tmp_files_dir = os.path.join(cur_dir, 'client_scripts', 'tmp_files')
    tests_dir = os.path.join(tmp_files_dir, 'tests')
    os.makedirs(tests_dir, exist_ok=True)

    output_ordered_chain_path = os.path.join(tests_dir, 'pytest-ordered_chain.pem')
    leaf_cert_file = os.path.join(tests_dir, 'test-leaf-cert.pem')
    measurements_file = os.path.join(tests_dir, 'test-measurements.json')
    nonce_file = os.path.join(tests_dir, 'test-nonce.txt')

    files_to_remove = []
    remove_test_files = False

    try:
        with allure.step('validate certificates chain and extract leaf to file'):
            validate_certs_chain(chain_str=cert_chain_content, output_leaf_path=leaf_cert_file,
                                 output_chain_path=output_ordered_chain_path)

        with allure.step('verify measurements using leaf certificate'):
            with allure.step('prepare measurements json file'):
                with open(measurements_file, 'w') as file:
                    json.dump(measurements_data, file, indent=4)
                files_to_remove.append(measurements_file)
            with allure.step('prepare nonce in txt file'):
                with open(nonce_file, 'w') as file:
                    file.write(nonce)
                files_to_remove.append(nonce_file)
            with allure.step('run measurements verification'):
                actual = True
                try:
                    verify_spdm_measurements(measurements_file, leaf_cert_file, nonce_file)
                except Exception as e:
                    actual = False
                    logging.info(f"ERROR: {str(e)}\n{traceback.format_exc()}")
            with allure.step(f'assert actual verification res ({actual}) is as expected ({expect_success})'):
                assert actual == expect_success, f'SPDM measurements result not as expected\nexpected: {expect_success}\nactual: {actual}'
    finally:
        if files_to_remove and remove_test_files:
            with allure.step('remove new files created in test'):
                for file in files_to_remove:
                    os.remove(file)


def compare_cert_chains_except_for_leaf(chain1: str, chain2: str):
    chain1_file, chain2_file = '/tmp/chain1.pem', '/tmp/chain2.pem'
    save_ordered_chain(order_certs(parse_certs_str(chain1))[1:], chain1_file)
    save_ordered_chain(order_certs(parse_certs_str(chain2))[1:], chain2_file)
    diff = CmdRunner().run_cmd(f'diff {chain1_file} {chain2_file}')
    assert not diff, f'chains are not identical as expected.\ndiff: {diff}'
