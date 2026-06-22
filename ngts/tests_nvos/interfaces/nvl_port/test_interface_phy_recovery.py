import random

import pytest
import logging
from retry.api import retry_call

import time
import re

from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import ApiType, NvosConst
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.cluster.cluster_tools import summarize_switch_ports
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.tests_nvos.interfaces.nvl_port.helpers import (
    reset_gpus_if_needed,
    restore_nvl_speed,
    select_random_nvl_port_name,
    skip_if_no_access_links,
    skip_if_no_trunk_links,
    validate_ports_state
)
from ngts.tests_nvos.interfaces.nvl_port.phy_recovery_helpers import (
    PHY_RECOVERY_HIGHER_TIMEOUT_MAX,
    PHY_RECOVERY_HIGHER_TIMEOUT_MIN,
    PHY_RECOVERY_LOWER_TIMEOUT_MAX,
    PHY_RECOVERY_LOWER_TIMEOUT_MIN,
    PHY_RECOVERY_TIMEOUT_STEP,
    PHY_RECOVERY_VERIFY_RECHECK_SECONDS,
    phy_recovery_apply_mode,
    phy_recovery_apply_neg_type,
    phy_recovery_apply_timeout,
    phy_recovery_test_profile,
    setup_nvl_speed_for_phy_recovery,
    validate_default_config,
    verify_phy_recovery_config,
    verify_phy_recovery_config_after_wait,
)
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import GnmiMode, GnmicErr
from ngts.tests_nvos.system.gnmi.helpers import verify_msg_not_in_out_or_err, verify_msg_in_out_or_err
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import PhyRecoveryConsts, NvosConsts
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.constants import MINUTE

logger = logging.getLogger()


# Constants for expected error messages
ERR_MSG_TIMEOUT_NEGATIVE_MIN = "-1 is less than the minimum of 0"
ERR_MSG_TIMEOUT_VALID_RANGE = "Valid range for serdes-eq-timeout is 0 - 2550"
ERR_MSG_BAD_MODE = "'bad-mode' is not one of"

# Command prefix for phy-recovery set command
NV_SET_FAE_INTERFACE_PHY_RECOVERY = "nv set fae interface {port} link phy-recovery"


@pytest.mark.interface
@pytest.mark.multiplanar
def test_phy_recovery_counters(engines, devices, random_api):
    """
    @summary:
        Verify default recovery counters via nv "show interface" and GNMI subscription.

    Steps:
    1. Set access ports to a random NVL speed other than 400G (phy recovery not supported at 400G).
    2. Select a random port for test.
    3. Run `nv show interface <port> link phy detail --view detailed` and parse JSON output.
    4. Confirm all default counters match default_phy_recovery_counters.
    5. Pick a random counter, subscribe via GNMI ONCE to 'phy-diag/state/<counter>'.
    6. Verify GNMI stream contains the counter name and its default value.
    """
    speed_info = setup_nvl_speed_for_phy_recovery(devices, required=True)
    try:
        with allure.step("Select a port for test"):
            port_result = RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_ALL_TYPES)
            if not port_result.result:
                pytest.skip(f"Skipping test - {port_result.info}")
            selected_port = port_result.get_returned_value()

        with allure.step("Select a random counter"):
            counters_list = list(devices.dut.default_phy_recovery_counters.keys())
            random_counter = random.choice(counters_list)
            path = f"phy-diag/state/{random_counter}"
            if random_counter in [PhyRecoveryConsts.LAST_RS_FEC_UNCORRECTABLE_DURING_RECOVERY,
                                  PhyRecoveryConsts.TOTAL_RS_FEC_UNCORRECTABLE_DURING_RECOVERY,
                                  PhyRecoveryConsts.LAST_SUCCESSFUL_RECOVERY_TIME,
                                  PhyRecoveryConsts.TOTAL_SUCCESSFUL_RECOVERY_TIME,
                                  PhyRecoveryConsts.LAST_SUCCESSFUL_RECOVERY_STEP_ATTEMPTS]:
                path = f"phy/recovery-information/state/{random_counter}"
            allure.attach(random_counter)

        with allure.step(f"Set up gnmi client and subscribe client to counter: {random_counter}"):
            client = GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, devices.dut.default_username,
                                devices.dut.default_password, verify_tools_installed=True)
            out, err = client.gnmic_subscribe_interface(GnmiMode.ONCE, selected_port.name, skip_cert_verify=True,
                                                        interface_path=path)

        with allure.step(f"Check that '{random_counter}' was streamed"):
            verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, out, err)
            verify_msg_in_out_or_err(f'{random_counter}', out)
    finally:
        restore_nvl_speed(speed_info)


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.timeout(15 * MINUTE)
def test_phy_recovery_attributes(devices, random_api):
    """
    @summary:
        Verify default recovery attributes via nv "show interface".

    Steps:
    1. Verify tested device is NVL6.
    2. Set access ports off 400G, then duplex on the test port (phy recovery not supported at 400G).
    3. Select a random port for test.
    4. Run `nv show fae interface <port> link phy-recovery --view detailed` and parse JSON output.
    5. Confirm all default attributes match default_phy_recovery_attributes.
    6. Pick a random attribute, set and verify.
    """

    attribute_changed = False
    preset_mode_changed = False

    port_names = getattr(devices.dut, 'nvl_access_ports_list', [])
    fae_port_names = Fae(port_name=summarize_switch_ports(port_names))
    prefix = re.match(r'[a-zA-Z]+', port_names[0]).group() if port_names else 'acp'

    try:
        with allure.step("Select a port for test"):
            port_result = RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_UP)
            if not port_result.result:
                pytest.fail(f"Failed to select a port - {port_result.info}")
            selected_port = Fae(port_name=port_result.get_returned_value().name)

        with allure.step("Validate attributes have default values"):
            port_output = OutputParsingTool.parse_json_str_to_dictionary(
                selected_port.port.interface.link.phy_recovery.show()).get_returned_value()
            default_values = devices.dut.default_phy_recovery_attributes
            _validate_default_values(port_output, default_values)

        with allure.step("Set step-2 preset-mode to full-duplex"):
            preset_mode_path = [PhyRecoveryConsts.STEP_2, PhyRecoveryConsts.PRESENT_MODE]
            _set_phy_recovery_attribute(
                fae_port_names.port.interface.link.phy_recovery,
                preset_mode_path,
                PhyRecoveryConsts.PRESENT_MODE,
                PhyRecoveryConsts.FULL_DUPLEX,
                apply=True,
                ask_for_confirmation=True,
            ).verify_result()
            time.sleep(30)
            retry_call(
                validate_ports_state,
                [port_names, prefix, NvosConsts.LINK_STATE_UP],
                exceptions=AssertionError, tries=13, delay=30
            )
            retry_call(
                _verify_attribute,
                [selected_port, preset_mode_path, PhyRecoveryConsts.PRESENT_MODE, PhyRecoveryConsts.FULL_DUPLEX],
                exceptions=AssertionError, tries=5, delay=5
            )
            preset_mode_changed = True

        with allure.step("Select a random mutable attribute"):
            port_output = OutputParsingTool.parse_json_str_to_dictionary(
                selected_port.port.interface.link.phy_recovery.show()).get_returned_value()
            random_path_list, random_attribute, current_value, new_lower_value, new_higher_value = _get_random_attribute_and_value(devices, port_output,
                                                                                                                                   PhyRecoveryConsts.phy_recovery_mutable_attributes,
                                                                                                                                   PhyRecoveryConsts.phy_recovery_attributes_options)

        with allure.step(f"Set {random_attribute} to {new_lower_value} on {selected_port.port.name} and verify value did not change"):
            _set_phy_recovery_attribute(
                selected_port.port.interface.link.phy_recovery,
                random_path_list, random_attribute, new_lower_value,
                apply=True, ask_for_confirmation=True,
            ).verify_result()
            with allure.step("Verify value did not change"):
                time.sleep(30)
                retry_call(
                    validate_ports_state,
                    [port_names, prefix, NvosConsts.LINK_STATE_UP],
                    exceptions=AssertionError, tries=13, delay=30
                )
                retry_call(
                    _verify_attribute,
                    [selected_port, random_path_list, random_attribute, str(current_value)],
                    exceptions=AssertionError, tries=5, delay=5
                )

        with allure.step(f"Set {random_attribute} to {new_higher_value} on all ports and verify"):
            _set_phy_recovery_attribute(
                fae_port_names.port.interface.link.phy_recovery,
                random_path_list, random_attribute, new_higher_value,
                apply=True, ask_for_confirmation=True,
            ).verify_result()
            time.sleep(30)
            retry_call(
                validate_ports_state,
                [port_names, prefix, NvosConsts.LINK_STATE_UP],
                exceptions=AssertionError, tries=13, delay=30
            )
            retry_call(
                _verify_attribute,
                [selected_port, random_path_list, random_attribute, str(new_higher_value)],
                exceptions=AssertionError, tries=5, delay=5
            )
            attribute_changed = True

    finally:
        if attribute_changed:
            if random_path_list[0] == random_attribute:
                previous_value = port_output[random_attribute]
            else:
                previous_value = port_output[random_path_list[0]][random_attribute]

            with allure.step(f"Set {random_attribute} to {previous_value}"):
                _set_phy_recovery_attribute(
                    fae_port_names.port.interface.link.phy_recovery,
                    random_path_list, random_attribute, previous_value,
                    apply=True, ask_for_confirmation=True,
                )

        if preset_mode_changed:
            with allure.step(f"Restore step-2 {PhyRecoveryConsts.PRESENT_MODE} to default"):
                _set_phy_recovery_attribute(
                    fae_port_names.port.interface.link.phy_recovery,
                    preset_mode_path,
                    PhyRecoveryConsts.PRESENT_MODE,
                    devices.dut.default_phy_recovery_attributes[PhyRecoveryConsts.STEP_2][PhyRecoveryConsts.PRESENT_MODE],
                    apply=True,
                    ask_for_confirmation=True,
                )


@pytest.mark.interface
@pytest.mark.multiplanar
def test_phy_recovery_bad_flow(devices, random_api):
    """
    @summary:
        Negative validation of phy-recovery parameters - reject invalid values.

    Steps:
    1. Set access ports to a random NVL speed other than 400G (phy recovery not supported at 400G).
    2. Attempt to show a non-existent phy-recovery attribute; expect failure.
    3. For both serdes-eq-mode and serdes-eq-timeout (when supported on DUT):
       a. Set the field to its bad_value with apply=True and ask_for_confirmation=True.
       b. Verify that the expected_error is raised.
    """
    speed_info = setup_nvl_speed_for_phy_recovery(devices, required=True)
    try:
        with allure.step("Select a port for test"):
            port_result = RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_UP)
            if not port_result:
                port_result.verify_result(False)
                port_result = RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_ALL_TYPES)
                if not port_result:
                    pytest.skip(f"Skipping test - {port_result.info}")
            selected_port = Fae(port_name=port_result.get_returned_value().name)

        phy_recovery_obj = selected_port.port.interface.link.phy_recovery
        with allure.step("Start bad-flow scenarios"):
            with allure.independent_step("Testing show non-existing attribute in phy-recovery"):
                phy_recovery_obj.show('non-existing', should_succeed=False)

            if devices.dut.asic_type in [NvosConst.QTM4, NvosConst.NVL6] and not is_bug_active(4635940):
                with allure.independent_step("Configure phy-recovery on selected port and verify port state can't get up"):
                    state = OutputParsingTool.parse_json_str_to_dictionary(selected_port.port.interface.link.state.show()).get_returned_value()
                    if state.get(NvosConsts.LINK_STATE_UP) is not None:
                        profile = phy_recovery_test_profile(devices)
                        phy_recovery_apply_mode(selected_port, profile.flow, PhyRecoveryConsts.ENABLED)
                        _verify_link_state(selected_port, NvosConsts.LINK_STATE_DOWN)
                    else:
                        logger.info("No port in up state were found")

            if f"{NV_SET_FAE_INTERFACE_PHY_RECOVERY} {PhyRecoveryConsts.SerdesEQ.MODE}" not in devices.dut.unsupported_commands_list:
                with allure.independent_step(f"Testing bad-mode on interface {selected_port.port.name}"):
                    logger.info(f"Set {PhyRecoveryConsts.SerdesEQ.MODE} to bad-mode")
                    phy_recovery_obj.set(PhyRecoveryConsts.SerdesEQ.MODE, "bad-mode", expected_str=ERR_MSG_BAD_MODE).verify_result()

            if f"{NV_SET_FAE_INTERFACE_PHY_RECOVERY} {PhyRecoveryConsts.SerdesEQ.TIMEOUT}" not in devices.dut.unsupported_commands_list:
                with allure.independent_step(f"Testing bad-timeout on interface {selected_port.port.name}"):
                    logger.info(f"Set {PhyRecoveryConsts.SerdesEQ.TIMEOUT} to -1")
                    expected_str = (
                        ERR_MSG_TIMEOUT_NEGATIVE_MIN
                        if is_bug_active(4631963) and random_api == ApiType.OPENAPI
                        else ERR_MSG_TIMEOUT_VALID_RANGE
                    )
                    phy_recovery_obj.set(PhyRecoveryConsts.SerdesEQ.TIMEOUT, -1, expected_str=expected_str).verify_result()
    finally:
        restore_nvl_speed(speed_info)


@pytest.mark.interface
@pytest.mark.multiplanar
def test_set_fae_phy_recovery_trunk_ports(devices, random_api):
    """
    @summary:
        Verify that firmware recovery settings (mode and timeout) can be applied and updated on trunk ports (sw).

    Steps:
    1. Skip if no trunk links.
    2. Enable recovery mode and set timeout on all trunk ports.
    3. Verify settings on a single random trunk port.
    4. Update timeout higher then lower, verifying group vs. local effects (NVL5: local lower unchanged;
       NVL6: local higher unchanged).
    5. Disable recovery and confirm defaults restored.
    """
    skip_if_no_trunk_links(devices)
    summarized_switch_ports = summarize_switch_ports(devices.dut.nvl_trunk_ports_list)
    speed_info = setup_nvl_speed_for_phy_recovery(
        devices, required=True,
    )
    try:
        port_name = select_random_nvl_port_name(devices, 'sw')
        _run_fae_mode_timeout_test(
            devices,
            group_all_ports=summarized_switch_ports,
            port_name=port_name,
        )
    finally:
        restore_nvl_speed(speed_info)


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.timeout(18 * MINUTE)
def test_set_fae_phy_recovery_access_ports(devices, random_api, standalone_system, has_loopbox, setup_name, is_simx):
    """
    @summary:
        Verify that firmware recovery settings (mode and timeout) can be applied and updated on access ports (acp).

    Steps:
    1. Skip if no access links.
    2. Set access ports to a random NVL speed other than 400G (phy recovery not supported at 400G).
    3. Enable recovery mode and set timeout on all access ports.
    4. Verify settings on a single random access port.
    5. Update timeout higher then lower, verifying group vs. local effects (NVL5: local lower unchanged;
       NVL6: local higher unchanged).
    6. Disable recovery and confirm defaults restored.

    Mode/timeout behavior uses phy_recovery_test_profile(devices) (NVL5 vs NVL6).
    """
    skip_if_no_access_links(has_loopbox, standalone_system, is_simx)
    speed_info = setup_nvl_speed_for_phy_recovery(devices, required=True)
    try:
        port_name = select_random_nvl_port_name(devices, 'acp')
        _run_fae_mode_timeout_test(
            devices,
            group_all_ports=f'acp1-{str(len(devices.dut.nvl_access_ports_list))}',
            port_name=port_name,
            standalone_system=standalone_system,
            setup_name=setup_name,
        )
    finally:
        restore_nvl_speed(speed_info)


# ==================== Mode / timeout (PhyRecoveryTestProfile: NVL5 vs NVL6) ====================


def _run_fae_mode_timeout_test(devices, group_all_ports, port_name, standalone_system=False, setup_name=None):
    """
    Shared trunk/access steps: phy_recovery_test_profile(devices) selects NVL5 vs NVL6.

    After raising timeout on the group: NVL5 rejects a smaller per-port timeout (group wins);
    NVL6 rejects a larger per-port timeout (group wins). Non-standalone NVL6 then applies a
    smaller per-port timeout under force-peer negotiation.
    """
    profile = phy_recovery_test_profile(devices)
    flow = profile.flow
    selected_port = Fae(port_name=port_name)
    all_ports = Fae(port_name=group_all_ports)

    validate_default_config(selected_port, devices)

    try:
        for mode in flow.modes:
            with allure.step(f"Set {flow.recovery_field} to {mode}"):
                if mode == PhyRecoveryConsts.ENABLED:
                    phy_recovery_apply_mode(all_ports, flow, mode)
                    reset_gpus_if_needed(setup_name)
                    if profile.is_nvl6() and not standalone_system:
                        with allure.step(
                            f"Set {PhyRecoveryConsts.RECOVERY_NEGATIVE_TYPE} to "
                            f"{PhyRecoveryConsts.RECOVERY_NEG_TYPE_FORCE_PEER} (non-standalone)"
                        ):
                            phy_recovery_apply_neg_type(all_ports, PhyRecoveryConsts.RECOVERY_NEG_TYPE_FORCE_PEER)
                            reset_gpus_if_needed(setup_name)
                    verify_phy_recovery_config(
                        selected_port, flow, mode, flow.default_timeout_when_enabled,
                    )

                    higher_timeout = random.randrange(
                        PHY_RECOVERY_HIGHER_TIMEOUT_MIN,
                        PHY_RECOVERY_HIGHER_TIMEOUT_MAX + PHY_RECOVERY_TIMEOUT_STEP,
                        PHY_RECOVERY_TIMEOUT_STEP,
                    )
                    with allure.step(
                        f"Update {flow.timeout_field} to higher value ({higher_timeout}) while mode {mode}"
                    ):
                        phy_recovery_apply_timeout(all_ports, flow, higher_timeout)
                        reset_gpus_if_needed(setup_name)
                        verify_phy_recovery_config(selected_port, flow, mode, higher_timeout)

                    lower_timeout = random.randrange(
                        PHY_RECOVERY_LOWER_TIMEOUT_MIN,
                        PHY_RECOVERY_LOWER_TIMEOUT_MAX + PHY_RECOVERY_TIMEOUT_STEP,
                        PHY_RECOVERY_TIMEOUT_STEP,
                    )
                    if not profile.is_nvl6():
                        with allure.step(
                            f"Update {flow.timeout_field} to lower value ({lower_timeout}) locally on one port — "
                            f"expect NO change (NVL5 keeps group {higher_timeout})"
                        ):
                            phy_recovery_apply_timeout(selected_port, flow, lower_timeout)
                            verify_phy_recovery_config_after_wait(
                                selected_port,
                                flow,
                                mode,
                                higher_timeout,
                                PHY_RECOVERY_VERIFY_RECHECK_SECONDS,
                            )
                    else:
                        with allure.step(
                            f"Update {flow.timeout_field} to larger value ({higher_timeout}) locally on one "
                            f"port — expect NO change (NVL6 keeps group {higher_timeout})"
                        ):
                            phy_recovery_apply_timeout(selected_port, flow, higher_timeout)
                            verify_phy_recovery_config_after_wait(
                                selected_port,
                                flow,
                                mode,
                                higher_timeout,
                                PHY_RECOVERY_VERIFY_RECHECK_SECONDS,
                            )

                    if profile.is_nvl6() and not standalone_system:
                        with allure.step(
                            f"Update {flow.timeout_field} to lower value ({lower_timeout}) locally — expect value applied"
                        ):
                            phy_recovery_apply_timeout(selected_port, flow, lower_timeout)
                            reset_gpus_if_needed(setup_name)
                            verify_phy_recovery_config(selected_port, flow, mode, lower_timeout)

                    with allure.step(f"Update {flow.timeout_field} to lower value ({lower_timeout}) on all ports"):
                        phy_recovery_apply_timeout(all_ports, flow, lower_timeout)
                        reset_gpus_if_needed(setup_name)
                        verify_phy_recovery_config(selected_port, flow, mode, lower_timeout)

                elif mode == PhyRecoveryConsts.DISABLED:
                    ports_in_up_state = RandomizationTool.select_random_ports(
                        requested_ports_state=NvosConsts.LINK_STATE_ALL_TYPES,
                        num_of_ports_to_select=0,
                    ).get_returned_value()
                    phy_recovery_apply_mode(all_ports, flow, mode)
                    verify_phy_recovery_config(selected_port, flow, mode)
                    current_ports_in_up_state = RandomizationTool.select_random_ports(
                        requested_ports_state=NvosConsts.LINK_STATE_ALL_TYPES,
                        num_of_ports_to_select=0,
                    ).get_returned_value()
                    expected_names = {port.name for port in ports_in_up_state}
                    current_names = {port.name for port in current_ports_in_up_state}
                    missing = sorted(expected_names - current_names)
                    extra = sorted(current_names - expected_names)
                    message = (
                        f"Port state mismatch after disabling recovery.\n"
                        f"Expected UP: {sorted(expected_names)}\n"
                        f"Actual UP: {sorted(current_names)}\n"
                        f"Missing: {missing}\n"
                        f"Extra: {extra}"
                    )
                    assert expected_names == current_names, message
                else:
                    phy_recovery_apply_mode(all_ports, flow, mode)
                    verify_phy_recovery_config(selected_port, flow, mode)
    finally:
        with allure.step("Unset configuration to return to defaults"):
            all_ports.port.interface.link.phy_recovery.unset(apply=True, ask_for_confirmation=True).verify_result()
            reset_gpus_if_needed(setup_name)

        retry_call(
            validate_default_config,
            fargs=[selected_port, devices],
            exceptions=AssertionError,
            tries=6,
            delay=30,
        )


def _validate_default_values(port_output, dict):
    """
    @summary:
        Validate fields and values from a port output against an expected dict.
    """
    expected_fields = list(dict.keys())
    expected_values = list(dict.values())
    ValidationTool.validate_fields_values_in_output(expected_fields, expected_values, port_output).verify_result()


def _verify_link_state(selected_port, expected_link_state):
    """
    @summary:
        Verify link state is applied to the selected port.
    """
    port_state = OutputParsingTool.parse_json_str_to_dictionary(
        selected_port.port.interface.link.state.show()).get_returned_value()
    assert port_state.get(expected_link_state) is not None, f"Link state is {port_state} but expected {expected_link_state}"


def _set_phy_recovery_attribute(phy_recovery, path_list, attribute, value, **kwargs):
    """Set a phy-recovery leaf via NVUE (``nv set ... step-N <attr> <value>``)."""
    if TestToolkit.tested_api == ApiType.OPENAPI and isinstance(value, str) and value.isdigit():
        value = int(value)
    if len(path_list) == 1 and path_list[0] == attribute:
        return phy_recovery.set(attribute, value, **kwargs)
    if TestToolkit.tested_api == ApiType.OPENAPI:
        return phy_recovery.set(path_list[0], {attribute: value}, **kwargs)
    return phy_recovery.set(path_list[0], [attribute, str(value)], **kwargs)


def _verify_attribute(selected_port, path, attribute, expected_value):
    port_output = OutputParsingTool.parse_json_str_to_dictionary(
        selected_port.port.interface.link.phy_recovery.show()).get_returned_value()
    if path[0] != attribute:
        port_output = port_output[path[0]]
    assert port_output.get(attribute) == expected_value, (
        f"{attribute} expected {expected_value}, got {port_output.get(attribute)}"
    )


def _flatten_attributes(d, parent_path=None):
    """
    Recursively flattens a nested dictionary to find all leaf attributes (keys).
    Returns a list of tuples: (full_path_list, attribute_key)
    Example: (['STEP_1', 'PRESENT_MODE'], 'PRESENT_MODE')
    """
    if parent_path is None:
        parent_path = []

    flat_list = []
    for k, v in d.items():
        current_path = parent_path + [k]
        if isinstance(v, dict):
            # Recurse for nested dictionaries
            flat_list.extend(_flatten_attributes(v, current_path))
        else:
            # Found a leaf attribute
            flat_list.append((current_path, k))
    return flat_list


def _get_random_attribute_and_value(devices, attributes_dict, mutable_attributes_dict, options_dict):
    """
    Chooses a random leaf attribute that has options defined, and selects
    a new random valid value for it.

    Args:
        attributes_dict (dict): The nested dictionary of current attributes.
        mutable_attributes_dict (dict) : The attributes that ate mutable.
        options_dict (dict): The dictionary containing valid options for each key.
    """
    # 1. Get all leaf attributes with their full path
    all_leaf_attributes = _flatten_attributes(attributes_dict)

    # 2. Filter attributes to include only those present in the options dictionary
    valid_attributes = [
        (path, key) for path, key in all_leaf_attributes
        if key in options_dict and key in mutable_attributes_dict
    ]

    # 3. Choose a random attribute (path, key) from the valid list
    random_path_list, attribute_key = random.choice(valid_attributes)

    # 4. Get the possible options and select a random new value
    possible_values = options_dict[attribute_key]
    if random_path_list[0] == attribute_key:
        current_value = attributes_dict[attribute_key]
    else:
        current_value = attributes_dict[random_path_list[0]][attribute_key]
    default_int = int(current_value)
    lower_candidates = [value for value in possible_values if value < default_int]
    higher_candidates = [value for value in possible_values if value > default_int]
    if not lower_candidates:
        lower_candidates = [default_int]
    if not higher_candidates:
        higher_candidates = [default_int]
    new_lower_value = random.choice(lower_candidates)
    new_higher_value = random.choice(higher_candidates)

    return random_path_list, attribute_key, current_value, new_lower_value, new_higher_value
