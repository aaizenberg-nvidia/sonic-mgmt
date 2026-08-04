import json
import logging
import os
import random

import pytest

from ngts.ngts_types import DevicesT
from ngts.nvos_constants.constants_nvos import ApiType, TestFlowType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.Spdm import SpdmComponentFields, SPDMComponents
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.bmc.bmc_erot_attestation.constants import NOT_EMPTY, VALID_NONCE_LEN, SpdmConsts
from ngts.tests_nvos.general.security.bmc.bmc_erot_attestation.helpers import (
    clean_cert_str,
    compare_cert_chains_except_for_leaf,
    get_component_obj,
    randomize_hex_str,
    randomize_non_hex_str,
    run_client_measurements_verification_usecanse,
    run_client_verification,
    verify_component_outputs,
)
from ngts.tools.test_utils import allure_utils as allure


@pytest.mark.bmc
@pytest.mark.erot
@pytest.mark.security
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_list_supported_components(test_api, devices, setup_name):
    """
    Verify that the general show command lists the supported components

    1.	run show spdm
    2.	verify output gives the supported components
    """
    TestToolkit.tested_api = test_api
    with allure.step("run show spdm"):
        out: dict = OutputParsingTool.parse_json_str_to_dictionary(System().security.spdm.show()).get_returned_value()
    with allure.step("verify output gives the supported components "):
        # Get device-specific expected SPDM components (not all platforms have all components)
        expected_components = devices.dut.get_spdm_components(setup_name)
        missing_components = [c for c in expected_components if c not in out]
        extra_components = [c for c in out.keys() if c not in expected_components]
        assert not missing_components and not extra_components, (
            f"show spdm content is wrong.\nexpected fields: {expected_components}\nactual: {list(out.keys())}"
        )


@pytest.mark.bmc
@pytest.mark.erot
@pytest.mark.security
def test_available_components(available_spdm_components, devices, setup_name):
    """
    Verify that available SPDM components (ERoTs, MCU, etc.) match device expectations
    """
    # Get device-specific expected SPDM components (already in proper format: 'ERoT_BMC_0', etc.)
    expected_spdm_components = devices.dut.get_spdm_components(setup_name)

    # If device doesn't define expected components, accept whatever is available (old devices)
    if not expected_spdm_components:
        logging.info("Device doesn't define expected SPDM components - accepting available ones as valid")
        expected_spdm_components = available_spdm_components

    # Verify all expected components are available
    missing_components = [comp for comp in expected_spdm_components if comp not in available_spdm_components]
    assert not missing_components, f"Expected SPDM components missing: {missing_components}. Available: {available_spdm_components}"

    # Verify no unexpected components (all available should be expected)
    unexpected_components = [comp for comp in available_spdm_components if comp not in expected_spdm_components]
    assert not unexpected_components, f"Unexpected SPDM components found: {unexpected_components}. Expected: {expected_spdm_components}"


@pytest.mark.bmc
@pytest.mark.erot
@pytest.mark.security
def test_main_flow(random_api, available_spdm_components, devices: DevicesT):
    """
    Verify that show component works properly and shows certificate chain

    1.  show spdm
    2.  verify all components exist as fields
    3.  verify inner outputs before generate
        1.  show component  #, component measurements, component certificate
        2.  verify fields [certificate, measurements] exist
        3.  show component certificate
        4,  verify fields [CertificateString, CertificateType, CertificateUsageTypes, Id, Name, SPDM] exist
        5.  show component measurements
        6,  verify fields [HashingAlgorithm, SignedMeasurements, SigningAlgorithm, Version] exist
        7.  check initial values
            - available device:     default algo/version (SignedMeasurements may be empty)
            - unavailable device:   N/As
        8.  sanity check on values
    3.  generate
    4.  verify inner outputs after generate
        1-6.sub steps 1-6 same as before generate (3)
        7.  check values
            - available device:     same certificate, non-empty SignedMeasurements
            - unavailable device:   N/As
        8.  sanity check on values
    """
    spdm = System().security.spdm

    with allure.step("show spdm"):
        out = OutputParsingTool.parse_json_str_to_dictionary(spdm.show()).get_returned_value()
    with allure.step(f"verify all components ({available_spdm_components}) exist as fields"):
        ValidationTool.verify_field_exist_in_json_output(out, available_spdm_components).verify_result()

    with allure.step("verify each component"):
        for component in available_spdm_components:
            with allure.independent_step(f"component: {component}"):
                component_obj = get_component_obj(component, available_spdm_components)
                component_is_available: bool = component in available_spdm_components
                with allure.step("verify inner outputs before generate"):
                    cert_out, _ = verify_component_outputs(component, component_obj, component_is_available, None, None)
                with allure.step(f"generate measurements - expect success: {component_is_available}"):
                    with allure.independent_step(f"Run generate without nonce param - expect success: {component_is_available}"):
                        res = component_obj.action_generate()
                        assert res.result == component_is_available, (
                            f"{component} - generate without nonce {'succeeded' if res.result else 'failed'} but expected to {'succeed' if component_is_available else 'fail'}"
                        )
                    with allure.independent_step(f"Run generate with valid nonce - expect success: {component_is_available}"):
                        rand_nonce = randomize_hex_str()
                        res = component_obj.action_generate(rand_nonce)
                        assert res.result == component_is_available, (
                            f"{component} - generate with valid nonce {'succeeded' if res.result else 'failed'} but expected to {'succeed' if component_is_available else 'fail'}"
                        )
                with allure.step("verify inner outputs after generate"):
                    verify_component_outputs(
                        component,
                        component_obj,
                        component_is_available,
                        expect_cert=cert_out[SpdmConsts.Component.Certificates.CERT_STRING],
                        expect_measurements=NOT_EMPTY,
                    )
                if component_is_available:
                    run_client_measurements_verification_usecanse(
                        component_obj, rand_nonce.lower()
                    )  # TODO: lower until talk with lev about request nonce and capitals


@pytest.mark.bmc
@pytest.mark.erot
@pytest.mark.security
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_generate_with_bad_nonce_param(test_api, available_spdm_components):
    """
    Verify that generate command rejected when giving invalid nonce

    1.	run generate with non-hex string as nonce param
    2.	expect failure
    3.	run generate with hex string longer than 64 chars
    4.	expect failure
    5.	run generate with hex string shorter than 64 chars
    6.	expect failure
    """
    TestToolkit.tested_api = test_api
    for component in available_spdm_components:
        component_obj = get_component_obj(component, available_spdm_components)
        with allure.step(f"component: {component}"):
            with allure.independent_step("run generate with empty nonce param (incomplete)"):
                bad_nonce = ""
                res = component_obj.action_generate(bad_nonce).ignore_result()
                assert not res.result, f"generate with empty nonce (incomplete) - success but expected to fail\n{res.info}"
            with allure.independent_step("run generate with non-hex string as nonce param"):
                bad_nonce = randomize_non_hex_str()
                res = component_obj.action_generate(bad_nonce).ignore_result()
                assert not res.result, f"generate with non hex nonce - success but expected to fail\n{res.info}"
            with allure.independent_step(f"run generate with hex string longer than {VALID_NONCE_LEN} chars"):
                bad_nonce = randomize_hex_str(random.randint(VALID_NONCE_LEN + 1, 2 * VALID_NONCE_LEN))
                res = component_obj.action_generate(bad_nonce).ignore_result()
                assert not res.result, f"generate with too long hex nonce - success but expected to fail\n{res.info}"
            with allure.independent_step(f"run generate with hex string shorter than {VALID_NONCE_LEN} chars"):
                bad_nonce = randomize_hex_str(random.randint(1, VALID_NONCE_LEN - 1))
                res = component_obj.action_generate(bad_nonce).ignore_result()
                assert not res.result, f"generate with too short hex nonce - success but expected to fail\n{res.info}"


@pytest.mark.bmc
@pytest.mark.erot
@pytest.mark.security
def test_generate_give_different_measurement(available_spdm_components):
    """
    Verify that when generating without nonce param multiple times it generates different measurements

    1.	generate without nonce
    2.	get measurements
    3.	generate without nonce
    4.	get measurements
    5.	verify measurements from step 2, 4 are different
    """
    for component in available_spdm_components:
        with allure.step(f"component: {component}"):
            component_obj = get_component_obj(component, available_spdm_components)
            with allure.step("generate without nonce"):
                component_obj.action_generate().verify_result()
            with allure.step("get measurements"):
                measurements1 = OutputParsingTool.parse_json_str_to_dictionary(component_obj.show()).get_returned_value()[
                    SpdmComponentFields.MEASUREMENTS
                ]
            with allure.step("generate without nonce"):
                component_obj.action_generate().verify_result()
            with allure.step("get measurements"):
                measurements2 = OutputParsingTool.parse_json_str_to_dictionary(component_obj.show()).get_returned_value()[
                    SpdmComponentFields.MEASUREMENTS
                ]
            with allure.step("verify measurements from step 2, 4 are different"):
                ValidationTool.compare_dictionaries(measurements1, measurements2).verify_result(False)


@pytest.mark.bmc
@pytest.mark.erot
@pytest.mark.security
@pytest.mark.track_serial_console
def test_reboot_system_keeps_data(available_spdm_components):
    """
    Verify that reboot keeps the results of the show

    1.	generate measurements for all components
    2.	run show and save outputs
    3.	reboot
    4.	run show components and compare outputs
    """
    available_spdm_components = [SPDMComponents.BMC]
    outputs_before_reboot = {}
    with allure.step("generate measurements and save show output for all components"):
        for component in available_spdm_components:
            with allure.step(f"component: {component}"):
                component_obj = get_component_obj(component, available_spdm_components)
                with allure.step("generate measurements for components"):
                    component_obj.action_generate().verify_result()
                with allure.step("save show output before reboot"):
                    out = OutputParsingTool.parse_json_str_to_dictionary(component_obj.show()).get_returned_value()
                    out[SpdmComponentFields.CERTIFICATES][SpdmConsts.Component.Certificates.CERT_STRING] = clean_cert_str(
                        out[SpdmComponentFields.CERTIFICATES][SpdmConsts.Component.Certificates.CERT_STRING]
                    )
                    outputs_before_reboot[component] = out
    with allure.step("reboot the system"):
        System().reboot.action_reboot(params="force").verify_result()
    with allure.step("run show components and compare outputs"):
        for component in available_spdm_components:
            with allure.independent_step(f"component: {component}"):
                component_obj = get_component_obj(component, available_spdm_components)
                with allure.step("show cert output after reboot"):
                    out = OutputParsingTool.parse_json_str_to_dictionary(component_obj.show()).get_returned_value()
                    out[SpdmComponentFields.CERTIFICATES][SpdmConsts.Component.Certificates.CERT_STRING] = clean_cert_str(
                        out[SpdmComponentFields.CERTIFICATES][SpdmConsts.Component.Certificates.CERT_STRING]
                    )
                with allure.step("compare cert show to output before reboot"):
                    actual_out_cert = out[SpdmComponentFields.CERTIFICATES]
                    expected_out_cert = outputs_before_reboot[component][SpdmComponentFields.CERTIFICATES]
                    with allure.independent_step(f"check all fields but {SpdmConsts.Component.Certificates.CERT_STRING} identical"):
                        ValidationTool.compare_dictionaries(
                            {k: v for k, v in expected_out_cert.items() if k != SpdmConsts.Component.Certificates.CERT_STRING},
                            {k: v for k, v in actual_out_cert.items() if k != SpdmConsts.Component.Certificates.CERT_STRING},
                        ).verify_result()
                    with allure.independent_step(f"check {SpdmConsts.Component.Certificates.CERT_STRING} identical except for leaf cert"):
                        compare_cert_chains_except_for_leaf(
                            expected_out_cert[SpdmConsts.Component.Certificates.CERT_STRING],
                            actual_out_cert[SpdmConsts.Component.Certificates.CERT_STRING],
                        )


@pytest.mark.parametrize("test_flow", TestFlowType.ALL_TYPES)
def test_dummy_attestation_verification(test_flow):
    """
    this is as unit-test to run_client_verification helper function, to make sure it fails for invalid nonce too
    """
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    test_is_good_flow = test_flow == TestFlowType.GOOD_FLOW
    correct_nonce = "5b017bcbe464aa0d5d4d029f7d6c77afee391d5e3f2ad7bdbc6045350d12bc35"
    invalid_nonce = "5b017bcbe464aa0d5d4d029f7d6c76afee391d5e3f2ad7bdbc6045350d12bc35"
    example_nonce = correct_nonce if test_is_good_flow else invalid_nonce
    example_component_data_json_file = os.path.join(cur_dir, "dummy_bmc_nvue_response.json")
    with allure.step("get component data"):
        with open(example_component_data_json_file, "r") as file:
            component_data = json.load(file)
    with allure.step("show cert chain"):
        cert_chain_str = component_data[SpdmComponentFields.CERTIFICATES][SpdmConsts.Component.Certificates.CERT_STRING]
        logging.info(f"certificate chain str:\n{cert_chain_str}")
    with allure.step("show measurements"):
        measurements_data = component_data[SpdmComponentFields.MEASUREMENTS]
        logging.info(f"measurements data:\n{measurements_data}")
    with allure.step("verify"):
        run_client_verification(cert_chain_str, measurements_data, example_nonce, test_is_good_flow)
