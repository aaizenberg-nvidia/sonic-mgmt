import logging
import time
import pytest

from ngts.nvos_constants.constants_nvos import ApiType, PlatformConsts, HealthConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.DutUtilsTool import RebootParams


cumulus_owner = "hiept"


logger = logging.getLogger()


@pytest.fixture(scope='function')
def required_for_redundancy(devices):
    if len(devices.dut.psu_list) == 0:
        pytest.skip("No PSUs found on the DUT")

    min_required = len(devices.dut.psu_list) // 2

    if devices.dut.is_eth():
        min_required = len(devices.dut.psu_list)

    required_for_redundancy = {PlatformConsts.PS_REDUNDANCY_NO: min_required,
                               PlatformConsts.PS_REDUNDANCY_GRID: min_required * 2,
                               PlatformConsts.PS_REDUNDANCY_PS: min_required + 1}
    return required_for_redundancy


def clear_platform_ps_redundancy(platform, engines):
    """
    Method to unset the platform ps-redundancy policy type
    :param platform:  Platform object
    :param engines: Engines object
    """

    with allure.step('Run unset platform ps-redundancy command and apply config'):
        platform.ps_redundancy.unset(apply=True, ask_for_confirmation=TestToolkit.devices.dut.ask_for_confirmation, dut_engine=engines.dut).verify_result()


@pytest.mark.platform
@pytest.mark.cumulus
def test_show_platform_ps_redundancy(random_api, devices, engines, min_psu_not_available_eth):
    """nv show platform ps-redundancy"""

    if min_psu_not_available_eth:
        with allure.step("Verify testcase applicable for cumulus devices"):
            pytest.skip("Skipping test_show_platform_ps_redundancy test on Cumulus devices as PS Redundancy is not supported because MIN_PSU is not available")

    with allure.step("Create Platform object"):
        platform = Platform()

    if len(devices.dut.psu_list) == 0:
        with allure.step("Check show platform ps-redundancy is not supported"):
            platform.ps_redundancy.show(should_succeed=False)
    else:
        with allure.step("Check show platform ps-redundancy output"):
            output = OutputParsingTool.parse_json_str_to_dictionary(platform.ps_redundancy.show()).get_returned_value()
            ValidationTool.verify_field_exist_in_json_output(output,
                                                             [PlatformConsts.PS_REDUNDANCY_POLICY,
                                                              PlatformConsts.PS_REDUNDANCY_MIN_REQ]).verify_result()


@pytest.mark.platform
@pytest.mark.cumulus
@pytest.mark.timeout(6 * MINUTE, func_only=True)
def test_set_platform_ps_redundancy(engines, random_api, required_for_redundancy, devices, min_psu_not_available_eth):
    """nv set platform ps-redundancy"""

    if min_psu_not_available_eth:
        with allure.step("Verify testcase applicable for cumulus devices"):
            pytest.skip("Skipping test_set_platform_ps_redundancy test on Cumulus devices as PS Redundancy is not supported because MIN_PSU is not available")

    with allure.step("Create Platform object"):
        platform = Platform()

    try:
        for policy_type in PlatformConsts.PS_REDUNDANCY_POLICY_TYPE:
            with allure.step("Set platform ps-redundancy to {} and verify in show output".format(policy_type)):
                platform.ps_redundancy.set(PlatformConsts.PS_REDUNDANCY_POLICY, policy_type,
                                           ask_for_confirmation=devices.dut.ask_for_confirmation, apply=True, dut_engine=engines.dut)
                output = OutputParsingTool.parse_json_str_to_dictionary(platform.ps_redundancy.show()).\
                    get_returned_value()
                ValidationTool.verify_field_value_in_output(output, PlatformConsts.PS_REDUNDANCY_POLICY,
                                                            policy_type).verify_result()
                ValidationTool.verify_field_value_in_output(output, PlatformConsts.PS_REDUNDANCY_MIN_REQ,
                                                            required_for_redundancy[policy_type]).verify_result()

        with allure.step("Unset platform ps-redundancy and verify show command shows default policy"):
            platform.ps_redundancy.unset(PlatformConsts.PS_REDUNDANCY_POLICY, apply=True, ask_for_confirmation=devices.dut.ask_for_confirmation, dut_engine=engines.dut).\
                verify_result()

            # To Do : Commenting the below code as the default policy is not defined yet.
            # output=OutputParsingTool.parse_json_str_to_dictionary(platform.ps_redundancy.show()).get_returned_value()
            # ValidationTool.verify_field_value_in_output(output, PlatformConsts.PS_REDUNDANCY_POLICY,
            #                                            PlatformConsts.PS_REDUNDANCY_POLICY_TYPE_DEF).verify_result()

    finally:
        clear_platform_ps_redundancy(platform, engines)


@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.cumulus
@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_platform_ps_redundancy_functionality(engines, devices, topology_obj, random_api, required_for_redundancy, min_psu_not_available_eth):

    if min_psu_not_available_eth:
        with allure.step("Verify testcase applicable for cumulus devices"):
            pytest.skip("Skipping test_platform_ps_redundancy_functionality test on Cumulus devices as PS Redundancy is not supported because MIN_PSU is not available")

    with allure.step("Create Platform object"):
        platform = Platform()

    with allure.step("Create System object"):
        system = System()

    try:
        with allure.step("Verify we have all PSUs "):
            available_psus = platform.environment.get_available_psus()
            if len(available_psus) < len(devices.dut.psu_list):
                pytest.skip(f"DUT has {len(available_psus)} valid PSUs, less than the required minimum for this test")

        with allure.step("Verify health is good and no issues are found"):
            system_health_check(system, HealthConsts.OK)

        with allure.step("Get the minimum number of PSUs required"):
            output = OutputParsingTool.parse_json_str_to_dictionary(platform.ps_redundancy.show()). \
                get_returned_value()
            assert PlatformConsts.PS_REDUNDANCY_MIN_REQ in output, "Platform ps-redundancy show does not show min-req"

        for policy_type in PlatformConsts.PS_REDUNDANCY_POLICY_TYPE:
            with allure.step("Set platform ps-redundancy to {} and verify in functionality".format(policy_type)):
                platform.ps_redundancy.set(PlatformConsts.PS_REDUNDANCY_POLICY, policy_type, apply=True,
                                           dut_engine=engines.dut)
                min_for_redundancy = required_for_redundancy[policy_type]
                platform_ps_redundancy_functionality(engines, topology_obj, system, policy_type, min_for_redundancy)
                logger.info("Policy {} is validated".format(policy_type))

    finally:
        clear_platform_ps_redundancy(platform, engines)


def platform_ps_redundancy_functionality(engines, topology_obj, system, policy_type, min_for_redundancy):
    try:
        with allure.step("Get name from NOGA"):
            noga_query_data = topology_obj.players['dut']['attributes'].noga_query_data['attributes']
            dhcp_hostname = noga_query_data['Common']['Name'] or noga_query_data['Specific']['dhcp_hostname']

        with allure.step("Deteriorate PSUs till redundancy threshold"):
            skip_str = PlatformConsts.PS_REBOOT_PSU_SKIP_STR
            psu_good_state = str(list(range(1, min_for_redundancy + 1))).replace('[', '').replace(']', '').replace(' ', '')
            psu_bad_state = str(list(range(1, min_for_redundancy))).replace('[', '').replace(']', '').replace(' ', '')
            skip_str_good = skip_str + psu_good_state + ' '
            skip_str_bad = skip_str + psu_bad_state + ' '
            DutUtilsTool.dut_psu_control(engines, topology_obj, skip_str_good, 'off', dhcp_hostname)
            # Wait for PSU states to update
            time.sleep(10)
            system_health_check(system, HealthConsts.OK)

            if policy_type != PlatformConsts.PS_REDUNDANCY_NO:
                with allure.step("Deteriorate one more PSU than redundancy threshold to fail PS redundancy"):
                    DutUtilsTool.dut_psu_control(engines, topology_obj, skip_str_bad, 'off', dhcp_hostname)
                    time.sleep(10)

                with allure.step("Validate System health is not OK and issues are found"):
                    system_health_check(system, HealthConsts.NOT_OK)

    finally:
        with allure.step("Recover PSUs"):
            DutUtilsTool.dut_psu_control(engines, topology_obj, '', 'on', dhcp_hostname)
            # Wait for PSU states to update
            time.sleep(10)

        with allure.step("Validate System health is OK and issues are not seen"):
            system_health_check(system, HealthConsts.OK)


def system_health_check(system, check_status):
    output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
    status = output[HealthConsts.STATUS]
    issues = output[HealthConsts.ISSUES]
    assert status == check_status, "System Health Status is {} instead of {}".format(status, check_status)
    if check_status is HealthConsts.OK:
        assert len(issues) == 0, "Unexpected issue found: {}".format(issues)
    else:
        assert len(issues) != 0, "Issues were expected in show health but not found"
