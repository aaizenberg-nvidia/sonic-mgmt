import pytest
import logging
import time
import re

from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts, IbInterfaceConsts, PhyHealthConsts, PhyRecoveryConsts
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.system.gnmi.helpers import verify_msg_not_in_out_or_err, parse_gnmi_output
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import GnmiMode, GnmicErr
from ngts.constants.constants import GnmiConsts
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.mark.gnmi
@pytest.mark.timeout(6 * MINUTE, func_only=True)
def test_gnmi_cli_interface_compare(engines, devices, random_api):
    """
    Compare CLI and GNMI interface outputs.
    Test flow:
    1. Randomize two ports (one in up-state and the second in down-state)
    2. Subscribe GNMI client to port.
    3. Run 'nv show interface <port>', and get cli output.
    4. Compare CLI and GNMI outputs.
    """
    tested_ports = []

    with allure.step("Select link-up port"):
        port_name = select_single_port_name()
        if port_name:
            tested_ports.append(port_name)

    with allure.step("Select link-down port"):
        port_name = select_single_port_name(NvosConsts.LINK_STATE_DOWN)
        if port_name:
            tested_ports.append(port_name)

    with allure.step("Get GNMI client"):
        client = GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, devices.dut.default_username,
                            devices.dut.default_password, verify_tools_installed=True)

    with allure.step("Start interface comparing"):
        for port in tested_ports:
            port_instance = Port(port)
            logger.info(f"Current port: {port}.")

            with allure.step("Sleep for 2 mins"):
                time.sleep(120)

            with allure.step("Start gnmi session and get output"):
                gnmi_prev_output_as_dict = {}
                logger.info("Pulling data every 1 seconds until we pull the latest data")
                gnmi_output_list = []
                for iteration in range(30):
                    gnmi_out, gnmi_err = client.gnmic_subscribe_interface(mode=GnmiMode.ONCE, interface_name=port,
                                                                          skip_cert_verify=True, wait_till_done=True,
                                                                          interface_path='')
                    verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, gnmi_out, gnmi_err)
                    gnmi_output_as_dict = parse_gnmi_output(gnmi_out)
                    gnmi_output_list.append(gnmi_output_as_dict)
                    if len(gnmi_prev_output_as_dict) == 0:
                        gnmi_prev_output_as_dict = gnmi_output_as_dict
                    if gnmi_output_as_dict != gnmi_prev_output_as_dict:
                        break
                    time.sleep(1)

            with allure.step(f"Run 'nv show interface {port}' command and get CLI output"):
                cli_output = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                    port_instance.interface.link.show()).get_returned_value()
                cli_output[IbInterfaceConsts.PHY_DETAIL] = Tools.OutputParsingTool.parse_show_output_to_dict(
                    port_instance.interface.link.phy.detail.show()).get_returned_value()

            with allure.step("Adjust CLI output to match GNMI output"):
                adjusted_cli_output = adjust_cli_attributes_and_values(devices.dut.interface_attributes_mapping_dict,
                                                                       cli_output)

            with allure.step("Compare GNMI-CLI relevant attributes"):
                if len(adjusted_cli_output) == 0:
                    logger.info("No interface attributes to compare")
                else:
                    if is_bug_active(4692220):
                        adjusted_cli_output.pop(PhyRecoveryConsts.LAST_RS_FEC_UNCORRECTABLE_DURING_RECOVERY, None)
                        adjusted_cli_output.pop(PhyRecoveryConsts.TOTAL_RS_FEC_UNCORRECTABLE_DURING_RECOVERY, None)
                        adjusted_cli_output.pop(PhyRecoveryConsts.LAST_SUCCESSFUL_RECOVERY_TIME, None)
                        adjusted_cli_output.pop(PhyRecoveryConsts.TOTAL_SUCCESSFUL_RECOVERY_TIME, None)
                        adjusted_cli_output.pop(PhyRecoveryConsts.LAST_SUCCESSFUL_RECOVERY_STEP_ATTEMPTS, None)
                    if is_bug_active(4566854):
                        adjusted_cli_output.pop(PhyHealthConsts.TIME_SINCE_LAST_CLEAR_MIN, None)
                    for attribute, value in adjusted_cli_output.items():
                        if is_bug_active(4835638) and value is None:
                            continue
                        with allure.independent_step(f"Testing {attribute}"):
                            assert attribute in gnmi_output_as_dict.keys(), f"Can't find {attribute} in GNMI output"
                            gnmi_value = gnmi_output_as_dict[attribute]
                            logger.info(f"CLI value = {value}, gnmi value = {gnmi_value}")
                            assert (str(gnmi_value).lower() == str(value).lower()) or handle_numeric_values(gnmi_value, value), f"Output mismatch"


def select_single_port_name(requested_ports_state=None):
    """
    Select a single port name using RandomizationTool with optional state.
    Logs the verification result on failure and returns None.
    """
    return_value = None
    result_obj = RandomizationTool.select_random_port(requested_ports_state=requested_ports_state)
    if not result_obj.result:
        result_obj.verify_result(False)
    else:
        port_obj = result_obj.verify_result(True)
        return_value = port_obj.name
    return return_value


def adjust_cli_attributes_and_values(attributes_mapping_dict, cli_output):
    """
    Adjust CLI attributes and values to match GNMI.
    (this is necessarily for the comparison process)
    """
    res = {}
    for attribute, value in cli_output.items():
        if isinstance(value, dict):
            for inner_attribute, inner_value in value.items():
                if inner_attribute in attributes_mapping_dict.keys():
                    res[attributes_mapping_dict[inner_attribute]] = adjust_cli_values(inner_attribute, inner_value)
                elif attribute in [IbInterfaceConsts.PHY_DIAG, IbInterfaceConsts.PHY_DETAIL]:
                    res[inner_attribute] = inner_value if inner_value is not "None" else "N/A"
        else:
            if attribute in attributes_mapping_dict.keys():
                res[attributes_mapping_dict[attribute]] = adjust_cli_values(attribute, value)
    return res


def adjust_speed(value):
    return 'SPEED_UNKNOWN' if value == '' else value.replace('G', '')


def adjust_ib_speed(value):
    if value == '':
        return 'SPEED_UNKNOWN'
    # Bug #5073236 (design): 'nv show interface' reports e.g. 'xdr' for ib-speed while the gNMI
    # subscription returns 'SPEED_XDR'. While the bug is open, align the CLI value to the gNMI
    # 'SPEED_<UPPER>' form so the comparison passes; once fixed (gNMI emits 'xdr'), the plain form matches.
    if is_bug_active(5073236):
        return 'SPEED_' + value.replace('G', '').upper()
    return value.replace('G', '')


def adjust_supported_speeds(value):
    speeds_list = value.split(',')
    tmp_list = [IbInterfaceConsts.SPEED_LIST[speed].replace('G', '') for speed in speeds_list]
    return ','.join([str(int(x) * 1000) for x in tmp_list])


def adjust_auto_negotiate(value):
    return 'true' if value in ['enabled'] else 'false'


def adjust_logical_state(value):
    return value.upper()


def adjust_physical_state(value):
    if value == IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP:
        return 'LINK_UP'
    elif value == IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_CONFIGURATION_TRAINING:
        return 'PORT_CONFIGURATION_TRAINING'
    else:
        return value.upper()


def adjust_supported_lanes(value):
    return value.replace(',', '_')


# Mapping cli-attributes to their corresponding adjustment functions
attribute_adjustments = {
    IbInterfaceConsts.LINK_SPEED: adjust_speed,
    IbInterfaceConsts.LINK_IB_SPEED: adjust_ib_speed,
    IbInterfaceConsts.LINK_SUPPORTED_SPEEDS: adjust_speed,
    IbInterfaceConsts.LINK_SUPPORTED_IB_SPEEDS: adjust_supported_speeds,
    IbInterfaceConsts.LINK_AUTO_NEGOTIATE: adjust_auto_negotiate,
    IbInterfaceConsts.LINK_LOGICAL_PORT_STATE: adjust_logical_state,
    IbInterfaceConsts.DHCP_STATE: adjust_logical_state,
    IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE: adjust_physical_state,
    IbInterfaceConsts.LINK_SUPPORTED_LANES: adjust_supported_lanes
}


def adjust_cli_values(attribute, value):
    adjustment_function = attribute_adjustments.get(attribute, None)
    if adjustment_function:
        return adjustment_function(value)
    return str(value) if not isinstance(value, str) else value


def handle_numeric_values(gnmi_value, cli_value):
    out = False
    if re.match(r'^\d*\.?\d+([eE][-+]?\d+)?$', gnmi_value) and re.match(r'^\d*\.?\d+([eE][-+]?\d+)?$', cli_value):
        gnmi_number = float(gnmi_value)
        assert gnmi_number >= 0, f"The value in gNMI is negative: {gnmi_number}"
        cli_number = float(cli_value)
        assert cli_number >= 0, f"The value in CLI is negative: {cli_number}"

        out = (abs(gnmi_number - cli_number) <= 0.1 * cli_number)
    return out
