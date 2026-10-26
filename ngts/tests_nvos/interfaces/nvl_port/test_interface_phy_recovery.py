import random

import pytest
import logging
from retry.api import retry_call

import time
import re

from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import ApiType, ConfState, NvosConst
from ngts.nvos_tools.Devices.cpo.CpoTopology import is_cpo_capable
from ngts.nvos_tools.infra.DeviceLogTool import mark_syslog
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.cluster.cluster_tools import summarize_switch_ports
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.interfaces.nvl_port.helpers import (
    reset_gpus_if_needed,
    restore_nvl_speed,
    select_random_nvl_port_name,
    skip_if_no_access_links,
    skip_if_no_trunk_links,
    validate_ports_state,
    wait_for_ports_state
)
from ngts.tests_nvos.interfaces.nvl_port.phy_recovery_helpers import (
    PHY_RECOVERY_HIGHER_TIMEOUT_MAX,
    PHY_RECOVERY_HIGHER_TIMEOUT_MIN,
    PHY_RECOVERY_LOWER_TIMEOUT_MAX,
    PHY_RECOVERY_LOWER_TIMEOUT_MIN,
    PHY_RECOVERY_TIMEOUT_STEP,
    PHY_RECOVERY_VERIFY_RECHECK_SECONDS,
    NVL7_TRUNK_LINK_UP_DELAY,
    NVL7_TRUNK_LINK_UP_TRIES,
    assert_one_log_line_since,
    is_nvl7,
    nvl7_access_operational,
    nvl7_assert_access_leaves_in_range,
    nvl7_clear_debug_counters,
    nvl7_enable_recovery,
    nvl7_link_admin_state,
    nvl7_missing_debug_counters,
    nvl7_new_leaves_present,
    nvl7_parse_phy_recovery,
    nvl7_read_debug_counters,
    nvl7_sai_rejects_since,
    nvl7_set_new_leaf,
    nvl7_stage_and_apply_leaves,
    nvl7_staged_config,
    nvl7_trigger_recovery,
    nvl7_trunk_port_names,
    nvl7_verify_new_leaves,
    nvl7_wait_trunk_up,
    nvl_port_flaps_since,
    nvl_port_sonic_name,
    phy_recovery_apply_mode,
    phy_recovery_apply_neg_type,
    phy_recovery_apply_timeout,
    phy_recovery_test_profile,
    select_nvl7_trunk_fae_port,
    set_phy_recovery_attribute,
    setup_nvl_speed_for_phy_recovery,
    validate_default_config,
    verify_phy_recovery_config,
    verify_phy_recovery_config_after_wait,
)
from ngts.tests_nvos.multi_asic.fatal_dump_rotation_helpers import SAI_DFW_DUMP_DIR_GLOB
from ngts.tests_nvos.system.factory_reset.pre_steps import get_health_status
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

SAI_FAILURE_DUMP_DIR = "/var/log/sai_failure_dump"
# iterations-preset3 of the ranged set: non-zero, so the tested port (back at 0 by then) changes and flaps
RANGED_STEP_1_VALUE = 4

# NVL7 CPO new-leaf bad-flow / pruning expected-error fragments live in PhyRecoveryConsts.NVL7_ERR_*
# (schema-shaped only). The trunk link-up wait budget lives in phy_recovery_helpers (nvl7_wait_trunk_up).


@pytest.mark.interfaces
@pytest.mark.simx
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

    Runs on all NVL flavors, including NVL7 NPO: per dev (2026-07-17) the phy-diag counters
    populate on NVL7 CPO; NPO behavior is unverified, so no skip - if NPO lacks the counters
    the failure will show the exact behavior and a gate can be added on that evidence.
    """
    # NVL7 supports only 200G, so the "avoid 400G" speed change is unnecessary and would
    # otherwise pytest.skip (no alternate speed). Only require the speed switch on NVL5/NVL6.
    speed_info = setup_nvl_speed_for_phy_recovery(devices, required=not is_nvl7(devices))
    try:
        with allure.step("Select a port for test"):
            port_result = RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_ALL_TYPES)
            if not port_result.result:
                # consume the failed ResultObj before raising: the autouse verify_result_objects
                # fixture errors at teardown on unconsumed failures, turning the skip into an ERROR
                port_result.ignore_result()
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
        restore_nvl_speed(devices, speed_info)


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(15 * MINUTE)
@pytest.mark.simx
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

    if is_nvl7(devices):
        # NVL7 has its own dedicated attributes test (test_nvl7_phy_recovery_attributes) with a
        # larger timeout budget; the NVL5/NVL6 generic flow below is not meant for NVL7.
        pytest.skip("NVL7 attributes are covered by test_nvl7_phy_recovery_attributes")

    attribute_changed = False
    preset_mode_changed = False

    port_names = getattr(devices.dut, 'nvl_access_ports_list', [])
    fae_port_names = Fae(port_name=summarize_switch_ports(port_names))
    prefix = re.match(r'[a-zA-Z]+', port_names[0]).group() if port_names else 'acp'

    try:
        with allure.step("Select a port for test"):
            port_result = RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_UP)
            if not port_result.result:
                # consume the failed ResultObj so it doesn't ALSO surface as a teardown error
                # from the autouse verify_result_objects fixture (fail already reports it here)
                port_result.ignore_result()
                pytest.fail(f"Failed to select a port - {port_result.info}")
            selected_port = Fae(port_name=port_result.get_returned_value().name)

        with allure.step("Validate attributes have default values"):
            port_output = OutputParsingTool.parse_json_str_to_dictionary(
                selected_port.port.interface.link.phy_recovery.show()).get_returned_value()
            default_values = devices.dut.default_phy_recovery_attributes
            _validate_default_values(port_output, default_values)

        with allure.step("Set step-2 preset-mode to full-duplex"):
            preset_mode_path = [PhyRecoveryConsts.STEP_2, PhyRecoveryConsts.PRESENT_MODE]
            set_phy_recovery_attribute(
                fae_port_names.port.interface.link.phy_recovery,
                preset_mode_path,
                PhyRecoveryConsts.PRESENT_MODE,
                PhyRecoveryConsts.FULL_DUPLEX,
                apply=True,
                ask_for_confirmation=True,
            ).verify_result()
            time.sleep(30)
            wait_for_ports_state(devices, port_names, prefix, NvosConsts.LINK_STATE_UP)
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
            set_phy_recovery_attribute(
                selected_port.port.interface.link.phy_recovery,
                random_path_list, random_attribute, new_lower_value,
                apply=True, ask_for_confirmation=True,
            ).verify_result()
            with allure.step("Verify value did not change"):
                time.sleep(30)
                wait_for_ports_state(devices, port_names, prefix, NvosConsts.LINK_STATE_UP)
                retry_call(
                    _verify_attribute,
                    [selected_port, random_path_list, random_attribute, str(current_value)],
                    exceptions=AssertionError, tries=5, delay=5
                )

        with allure.step(f"Set {random_attribute} to {new_higher_value} on all ports and verify"):
            set_phy_recovery_attribute(
                fae_port_names.port.interface.link.phy_recovery,
                random_path_list, random_attribute, new_higher_value,
                apply=True, ask_for_confirmation=True,
            ).verify_result()
            time.sleep(30)
            wait_for_ports_state(devices, port_names, prefix, NvosConsts.LINK_STATE_UP)
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
                set_phy_recovery_attribute(
                    fae_port_names.port.interface.link.phy_recovery,
                    random_path_list, random_attribute, previous_value,
                    apply=True, ask_for_confirmation=True,
                )

        if preset_mode_changed:
            with allure.step(f"Restore step-2 {PhyRecoveryConsts.PRESENT_MODE} to default"):
                set_phy_recovery_attribute(
                    fae_port_names.port.interface.link.phy_recovery,
                    preset_mode_path,
                    PhyRecoveryConsts.PRESENT_MODE,
                    devices.dut.default_phy_recovery_attributes[PhyRecoveryConsts.STEP_2][PhyRecoveryConsts.PRESENT_MODE],
                    apply=True,
                    ask_for_confirmation=True,
                )


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.simx
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
    # NVL7 (CPO) supports only 200G, so the "avoid 400G" speed change is unnecessary and would
    # otherwise pytest.skip (no alternate speed). Only require the speed switch on NVL5/NVL6.
    speed_info = setup_nvl_speed_for_phy_recovery(devices, required=not is_nvl7(devices))
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

            if is_cpo_capable(devices.dut):
                # NVL7 CPO new leaves: reject bad enum + out-of-range/negative/non-numeric.
                # Only meaningful on a trunk port where the new leaves are NOT pruned.
                _run_nvl7_new_leaves_bad_flow(devices)
    finally:
        restore_nvl_speed(devices, speed_info)


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.simx
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
    # skip_if_no_trunk_links skips all Juliet-derived devices, incl. every Portia (NVL7). NVL7 CPO
    # bulk/new-leaf trunk coverage therefore lives in the dedicated test_nvl7_phy_recovery_* tests;
    # this shared test remains NVL5/NVL6 scale-out only.
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
        restore_nvl_speed(devices, speed_info)


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(25 * MINUTE)
@pytest.mark.simx
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
        restore_nvl_speed(devices, speed_info)


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
                        selected_port, flow, mode, flow.default_timeout_when_enabled, retries=26, delay=15
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
                        verify_phy_recovery_config(selected_port, flow, mode, higher_timeout, retries=26, delay=15)

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
                            verify_phy_recovery_config(selected_port, flow, mode, lower_timeout, retries=26, delay=15)

                    with allure.step(f"Update {flow.timeout_field} to lower value ({lower_timeout}) on all ports"):
                        phy_recovery_apply_timeout(all_ports, flow, lower_timeout)
                        reset_gpus_if_needed(setup_name)
                        verify_phy_recovery_config(selected_port, flow, mode, lower_timeout, retries=26, delay=15)

                elif mode == PhyRecoveryConsts.DISABLED:
                    ports_in_up_state = RandomizationTool.select_random_ports(
                        requested_ports_state=NvosConsts.LINK_STATE_ALL_TYPES,
                        num_of_ports_to_select=0,
                    ).get_returned_value()
                    phy_recovery_apply_mode(all_ports, flow, mode)
                    verify_phy_recovery_config(selected_port, flow, mode, retries=26, delay=15)
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
                    verify_phy_recovery_config(selected_port, flow, mode, retries=26, delay=15)
    finally:
        with allure.step("Unset configuration to return to defaults"):
            all_ports.port.interface.link.phy_recovery.unset(apply=True, ask_for_confirmation=True).verify_result()
            reset_gpus_if_needed(setup_name)

        retry_call(
            validate_default_config,
            fargs=[selected_port, devices],
            exceptions=AssertionError,
            tries=26,
            delay=15
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


# =====================================================================================
# NVL7 CPO trunk-port PHY recovery: new leaves + per-step debug counters
# HLD: HLD_NVOS_CPO_PHY_RECOVERY_NVL7
# =====================================================================================


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(45 * MINUTE, func_only=True)
@pytest.mark.simx
def test_nvl7_phy_recovery_attributes(devices, random_api):
    """
    @summary (NVL7 CPO only):
        Validate phy-recovery attributes on a TRUNK port: inherited NVL6 defaults present, the 5 new
        leaves at their literal defaults, then a deterministic set-and-verify sweep (literal echo:
        every configured value round-trips exactly in show, including under preset policies). Split
        out from test_phy_recovery_attributes for its larger link-bounce timeout budget.
    """
    if not is_cpo_capable(devices.dut):
        pytest.skip("Test requires a CPO-capable device")
    _run_nvl7_phy_recovery_attributes(devices)


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.simx
def test_nvl7_phy_recovery_trunk_bulk(devices, random_api):
    """
    @summary (NVL7 CPO only):
        Bulk-set recovery-policy-config (explicit-config-controls) + one numeric timer leaf on the
        whole trunk range in one apply, verify on 2-3 ports, then bulk unset.

    Standalone NVL7 test (NOT gated by skip_if_no_trunk_links, which skips all Portia): the shared
    test_set_fae_phy_recovery_trunk_ports cannot run on Portia, so the CPO bulk coverage lives here.
    """
    if not is_cpo_capable(devices.dut):
        pytest.skip("Test requires a CPO-capable device")
    trunk_names = nvl7_trunk_port_names()
    if not trunk_names:
        pytest.skip("No NVL7 trunk (sw*) ports found")
    _run_nvl7_bulk_trunk_new_leaves(trunk_names)


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(25 * MINUTE)
@pytest.mark.simx
def test_nvl7_phy_recovery_counters_debug(engines, devices, random_api):
    """
    @summary (NVL7 CPO only):
        Validate the FAE-only per-step debug counters:
        nv show fae interface <sw-port> counters debug

    Steps:
    1. Skip unless NVL7 CPO.
    2. FAE clear -> all six debug step-counters are 0 by default.
    3. Enable recovery (recovery-status enabled + robust-optimized policy) on a trunk port.
    4. Trigger recovery (go-once) -> step counters increment, time-in-last-step > 0, port stays up.
    5. Plain (non-fae) clear -> debug counters UNCHANGED (only the fae clear resets them).
    6. Set recovery-off + trigger -> counters unchanged.
    7. FAE clear -> counters back to zero.

    go-once trigger is confirmed wired (dev 2026-07-17); a rejected trigger fails the test.
    """
    if not is_cpo_capable(devices.dut):
        pytest.skip("Test requires a CPO-capable device")

    speed_info = setup_nvl_speed_for_phy_recovery(devices, required=False)
    selected_port = _require_nvl7_trunk_fae_port()
    port_name = selected_port.port.name
    try:
        with allure.step("Clear counters and verify all six debug step-counters are 0 by default"):
            nvl7_clear_debug_counters(selected_port, dut_engine=engines.dut)
            _verify_nvl7_debug_counters_default(selected_port, devices)

        with allure.step("Enable recovery (recovery-status enabled + robust-optimized policy)"):
            # recovery-status enabled is the master enable; robust-optimized selects behavior so the
            # go-once trigger actually runs recovery steps and increments the counters.
            nvl7_enable_recovery(selected_port, policy=PhyRecoveryConsts.RecoveryPolicyConfig.ROBUST_OPTIMIZED)
            nvl7_wait_trunk_up(port_name)

        with allure.step("Trigger recovery (go-once) and verify step counters increment"):
            before = nvl7_read_debug_counters(selected_port)
            # go-once is dev-confirmed wired on NVL7 CPO (2026-07-17): a rejected trigger is a
            # real failure, not a skip (no active bug to gate a skip on).
            nvl7_trigger_recovery(port_name).verify_result()
            nvl7_wait_trunk_up(port_name)
            _verify_nvl7_step_counters_incremented(selected_port, before)

        with allure.step("Plain (non-fae) clear must NOT reset the debug counters"):
            # Per dev (2026-07-17): only the fae-scoped clear resets the 6 debug counters.
            before_plain = nvl7_read_debug_counters(selected_port)
            nvl7_clear_debug_counters(selected_port, dut_engine=engines.dut, fae=False)
            after_plain = nvl7_read_debug_counters(selected_port)
            assert after_plain == before_plain, (
                f"Plain (non-fae) clear must not touch debug counters: before={before_plain} after={after_plain}"
            )

        with allure.step("Set recovery-off, trigger, and verify debug counters are unchanged"):
            nvl7_set_new_leaf(
                selected_port,
                PhyRecoveryConsts.RECOVERY_POLICY_CONFIG,
                PhyRecoveryConsts.RecoveryPolicyConfig.RECOVERY_OFF,
            )
            nvl7_wait_trunk_up(port_name)
            before_off = nvl7_read_debug_counters(selected_port)
            # The trigger outcome itself is not asserted here (we only check counters stay
            # unchanged), and FW may reject a trigger while recovery-off - consume the ResultObj.
            nvl7_trigger_recovery(port_name).ignore_result()
            time.sleep(PHY_RECOVERY_VERIFY_RECHECK_SECONDS)
            after_off = nvl7_read_debug_counters(selected_port)
            consts = PhyRecoveryConsts
            # Step COUNTS must not change on a no-op (recovery-off) trigger.
            for step_count in (consts.TOTAL_STEP_1_COUNT, consts.TOTAL_STEP_2_COUNT):
                assert after_off[step_count] == before_off[step_count], (
                    f"{step_count} changed while recovery-off: before={before_off} after={after_off}"
                )
            # Time counters may be touched by a FW-recorded no-op attempt; require only no-decrease.
            for time_counter in (consts.TIME_IN_LAST_STEP_1, consts.TIME_IN_LAST_STEP_2,
                                 consts.TOTAL_TIME_IN_STEP_1, consts.TOTAL_TIME_IN_STEP_2):
                assert after_off[time_counter] >= before_off[time_counter], (
                    f"{time_counter} decreased while recovery-off: before={before_off} after={after_off}"
                )

        with allure.step("Clear counters => all six debug step-counters back to zero"):
            nvl7_clear_debug_counters(selected_port, dut_engine=engines.dut)
            _verify_nvl7_debug_counters_default(selected_port, devices)
    finally:
        _nvl7_cleanup(selected_port)
        restore_nvl_speed(devices, speed_info)


@pytest.mark.interfaces
@pytest.mark.simx
@pytest.mark.multiplanar
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_nvl7_phy_recovery_pruning(devices):
    """
    @summary: Validate per-port-type pruning of the NVL7 phy-recovery leaves and the counters debug node.
        1. Trunk (sw*) port, when the device has any: the 5 new leaves and counters debug are present;
           the 6 step-1 access leaves are absent and a set of one is rejected.
        2. Access port: the 6 access leaves are present under step-1 (applied view), counters debug and
           recovery-policy-config are present; the 4 trunk-only leaves are absent (operational view).
        3. Access port: the 6 access leaves are absent outside the FAE namespace (link phy-recovery and link).
        4. Access port: all six debug counters are at their defaults (no recovery since boot).
        5. Access port: a set of a trunk-only leaf is rejected.
        6. eth0, lo and an fnm port: all phy-recovery leaves and counters debug are absent and a set is rejected.
    """
    if not is_nvl7(devices):
        pytest.skip("NVL7 only test")
    consts = PhyRecoveryConsts

    trunk_names = nvl7_trunk_port_names(NvosConsts.LINK_STATE_ALL_TYPES)
    if trunk_names:
        trunk = Fae(port_name=random.choice(trunk_names))
        with allure.step(f"Trunk port {trunk.port.name}: new leaves + counters debug present, access leaves pruned"):
            all_present, missing = nvl7_new_leaves_present(trunk)
            assert all_present, f"NVL7 new leaves missing on trunk {trunk.port.name}: {missing}"
            counters_missing = nvl7_missing_debug_counters(trunk)
            assert not counters_missing, f"debug counters missing on trunk {trunk.port.name}: {counters_missing}"
            _assert_nvl7_access_leaves_pruned(trunk)
    else:
        logger.info("No trunk (sw*) ports on this device, trunk checks skipped")

    acp = Fae(port_name=random.choice(devices.dut.nvl_access_ports_list))
    with allure.step(f"Access port {acp.port.name}: access leaves, counters debug and policy present, "
                     f"trunk-only leaves pruned"):
        applied_step_1 = nvl7_parse_phy_recovery(acp, ConfState.APPLIED)[consts.STEP_1]
        missing = [leaf for leaf in consts.NVL7_ACCESS_LEAVES if leaf not in applied_step_1]
        assert not missing, f"Access leaves missing from applied step-1 on {acp.port.name}: {missing}"
        counters_missing = nvl7_missing_debug_counters(acp)
        assert not counters_missing, f"debug counters missing on {acp.port.name}: {counters_missing}"
        _, missing = nvl7_new_leaves_present(acp)
        assert set(missing) == set(consts.NVL7_TRUNK_ONLY_LEAVES), (
            f"Expected exactly the trunk-only leaves pruned on {acp.port.name}, missing: {missing}"
        )

    with allure.step(f"Access port {acp.port.name}: access leaves absent outside the FAE namespace"):
        port = Port(acp.port.name)
        _, missing = nvl7_new_leaves_present(port, PhyRecoveryConsts.NVL7_ACCESS_LEAVES,
                                             subtree=PhyRecoveryConsts.STEP_1)
        shown = set(PhyRecoveryConsts.NVL7_ACCESS_LEAVES) - set(missing)
        assert not shown, f"Access leaves shown by non-FAE link phy-recovery on {acp.port.name}: {shown}"
        link = port.interface.link.show()
        shown = [leaf for leaf in PhyRecoveryConsts.NVL7_ACCESS_LEAVES if leaf in link]
        assert not shown, f"Access leaves shown by non-FAE link on {acp.port.name}: {shown}"

    with allure.step(f"Access port {acp.port.name}: debug counters at defaults"):
        _verify_nvl7_debug_counters_default(acp, devices)

    _assert_nvl7_pruned_set_rejected(acp, consts.RECOVERY_TX_TOGGLE_DELAY, 1)

    with allure.step("All phy-recovery leaves + counters debug are PRUNED on eth0/lo/fnm"):
        for port_name in _nvl7_non_nvl_link_ports(devices):
            with allure.independent_step(f"phy-recovery pruned on {port_name}"):
                fae_port = Fae(port_name=port_name)
                _assert_nvl7_leaves_pruned(fae_port, port_name)
                _assert_nvl7_access_leaves_pruned(fae_port)


@pytest.mark.interfaces
@pytest.mark.simx
@pytest.mark.multiplanar
@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.usefixtures('random_api')
def test_nvl7_access_phy_recovery_config(engines, devices, setup_name, nvl7_acp_phy_recovery):
    """
    @summary: Validate configuration of the NVL7 access-port step-1 recovery leaves on one end of an acp pair
        (on simx the two ports are cabled to each other).
        1. Applied defaults are 0; with the link up the operational leaves are within schema range and are
           recorded as the firmware defaults.
        2. Set iterations-preset3 and verify the applied value, that admin state is restored and that
           orchagent logged one notice for the value and no rejection.
        3. Set iterations-preset3 and consecutive-recovery-window-preset3 to the device caps, when known.
        4. Set an existing step-1 leaf and step-2 preset-mode and verify both are applied.
        5. Set all six leaves to their range maximum in one apply, then to 0 in one apply:
           applied shows the values and the port flaps once per apply.
        6. Set all six leaves to 1, unset each leaf in one apply: applied is back to 0 on all six.
        7. Set of each of the six leaves under step-2 is rejected and nothing is staged.
        8. Set iterations-preset3 on all access ports in one apply: applied on sampled ports,
           exactly one flap on a changed up port and none on an unchanged one.
        9. Generate tech-support and verify it holds the configured value from step 8, the operational
           step-1 leaves and the debug counters of the tested port.
        10. Unset on all access ports: sampled ports are back to applied 0 and the firmware-default operational.
    """
    consts = PhyRecoveryConsts
    engine = engines.dut
    acp_names = devices.dut.nvl_access_ports_list
    port_a, port_b = nvl7_acp_phy_recovery
    # The unchanged port must not be port_b: a flap of port_a takes its peer down too
    sampled = [port_a, random.choice([name for name in acp_names if name not in (port_a, port_b)]), port_b]
    changed_name = sampled[0]
    port = Fae(port_name=changed_name)
    all_ports = Fae(port_name=summarize_switch_ports(acp_names))

    with allure.step("Applied defaults are 0; operational leaves are within schema range (firmware defaults)"):
        nvl7_verify_new_leaves(port, consts.NVL7_ACCESS_LEAF_DEFAULTS, subtree=consts.STEP_1, applied=True)
        fw_defaults = {name: nvl7_access_operational(Fae(port_name=name)) for name in sampled}
        for name in sampled:
            nvl7_assert_access_leaves_in_range(name, fw_defaults[name])

    _apply_accepted_value(engine, devices, port, consts.ITERATIONS_PRESET3, 5)

    caps = devices.dut.phy_recovery_step1_caps
    if caps:
        with allure.step(f"Set the device caps {caps}"):
            nvl7_stage_and_apply_leaves(port, caps.items(), subtree=consts.STEP_1)
            nvl7_verify_new_leaves(port, caps, subtree=consts.STEP_1, applied=True)

    with allure.step("Set an existing step-1 leaf and step-2 preset-mode"):
        nvl7_stage_and_apply_leaves(port, [(consts.PEQ_NUMBER_OF_RETRY_PRESET3, 3)], subtree=consts.STEP_1)
        nvl7_stage_and_apply_leaves(port, [(consts.PRESENT_MODE, consts.FULL_DUPLEX)], subtree=consts.STEP_2)
        nvl7_verify_new_leaves(port, {consts.PEQ_NUMBER_OF_RETRY_PRESET3: 3}, subtree=consts.STEP_1, applied=True)
        nvl7_verify_new_leaves(port, {consts.PRESENT_MODE: consts.FULL_DUPLEX}, subtree=consts.STEP_2,
                               applied=True)

    _apply_range_ends_with_one_flap(engine, devices, port)
    _verify_per_leaf_unset(port)
    _assert_step_2_access_sets_rejected(engine, port)

    _apply_ranged_value(engine, devices, setup_name, all_ports, sampled)

    _verify_techsupport_holds_step_1_config(engine, changed_name, {consts.ITERATIONS_PRESET3: RANGED_STEP_1_VALUE})

    with allure.step(f"Ranged unset on {all_ports.port.name}: sampled ports back to defaults"):
        all_ports.port.interface.link.phy_recovery.unset(apply=True, ask_for_confirmation=True).verify_result()
        reset_gpus_if_needed(setup_name)
        _verify_applied_step_1(sampled, consts.NVL7_ACCESS_LEAF_DEFAULTS)
        wait_for_ports_state(devices, acp_names, 'acp')
        for name in sampled:
            nvl7_verify_new_leaves(Fae(port_name=name), fw_defaults[name], subtree=consts.STEP_1)


def _apply_ranged_value(engine, devices, setup_name, all_ports, sampled):
    """Ranged set on all acp ports; the second sampled port already holds the value and must not flap."""
    leaf = PhyRecoveryConsts.ITERATIONS_PRESET3
    value = RANGED_STEP_1_VALUE
    changed_name, unchanged_name = sampled[:2]
    with allure.step(f"Ranged set {leaf} {value} on {all_ports.port.name}"):
        nvl7_stage_and_apply_leaves(Fae(port_name=unchanged_name), [(leaf, value)], subtree=PhyRecoveryConsts.STEP_1)
        # Assumes the sampled acp ports are up (all are on simx via nvl_simx_setup)
        wait_for_ports_state(devices, [changed_name, unchanged_name], 'acp')
        marker = mark_syslog(engine)
        nvl7_stage_and_apply_leaves(all_ports, [(leaf, value)], subtree=PhyRecoveryConsts.STEP_1)
        reset_gpus_if_needed(setup_name)
        _verify_applied_step_1(sampled, {leaf: value})
        _assert_flap_only_on_changed_port(engine, marker, changed_name, unchanged_name)


def _verify_applied_step_1(port_names, expected):
    for name in port_names:
        nvl7_verify_new_leaves(Fae(port_name=name), expected, subtree=PhyRecoveryConsts.STEP_1, applied=True)


def _apply_accepted_value(engine, devices, fae_port, leaf, value):
    port_name = fae_port.port.name
    sonic_name = nvl_port_sonic_name(engine, port_name)
    notice = PhyRecoveryConsts.NVL7_SAI_ACCEPT_LOG.format(port=sonic_name, value=value)
    with allure.step(f"Set {leaf} {value}: applied, admin state restored, one notice and no rejection"):
        admin_state = nvl7_link_admin_state(fae_port)
        marker = mark_syslog(engine)
        nvl7_stage_and_apply_leaves(fae_port, [(leaf, value)], subtree=PhyRecoveryConsts.STEP_1)
        nvl7_verify_new_leaves(fae_port, {leaf: value}, subtree=PhyRecoveryConsts.STEP_1, applied=True)
        wait_for_ports_state(devices, [port_name], 'acp')
        assert nvl7_link_admin_state(fae_port) == admin_state, f"{port_name} admin state not restored after apply"
        assert_one_log_line_since(engine, marker, notice, f"notice of {leaf} {value} on {sonic_name}")
        rejects = nvl7_sai_rejects_since(engine, marker, sonic_name, [leaf])
        assert rejects == 0, f"{rejects} rejection(s) of {leaf} logged on {sonic_name}"


def _apply_range_ends_with_one_flap(engine, devices, fae_port):
    port_name = fae_port.port.name
    for values in (PhyRecoveryConsts.NVL7_ACCESS_LEAF_MAX, PhyRecoveryConsts.NVL7_ACCESS_LEAF_DEFAULTS):
        with allure.step(f"Set all six leaves in one apply {values}: applied, one flap"):
            wait_for_ports_state(devices, [port_name], 'acp')
            marker = mark_syslog(engine)
            nvl7_stage_and_apply_leaves(fae_port, values.items(), subtree=PhyRecoveryConsts.STEP_1)
            nvl7_verify_new_leaves(fae_port, values, subtree=PhyRecoveryConsts.STEP_1, applied=True)
            _wait_for_flaps(engine, marker, port_name, 1)


def _verify_per_leaf_unset(fae_port):
    leaves = PhyRecoveryConsts.NVL7_ACCESS_LEAVES
    with allure.step("Set all six leaves to 1, unset each leaf in one apply: applied back to 0"):
        nvl7_stage_and_apply_leaves(fae_port, [(leaf, 1) for leaf in leaves], subtree=PhyRecoveryConsts.STEP_1)
        nvl7_verify_new_leaves(fae_port, dict.fromkeys(leaves, 1), subtree=PhyRecoveryConsts.STEP_1, applied=True)
        step_1 = fae_port.port.interface.link.phy_recovery.step_1
        for leaf in leaves:
            is_last = leaf == leaves[-1]
            step_1.unset(leaf, apply=is_last, ask_for_confirmation=is_last).verify_result()
        nvl7_verify_new_leaves(fae_port, PhyRecoveryConsts.NVL7_ACCESS_LEAF_DEFAULTS, subtree=PhyRecoveryConsts.STEP_1,
                               applied=True)


def _verify_techsupport_holds_step_1_config(engine, port_name, applied):
    """Tech-support holds ``applied`` (other leaves at default), the shown operational leaves and the debug counters."""
    consts = PhyRecoveryConsts
    techsupport = System().techsupport
    sonic_name = nvl_port_sonic_name(engine, port_name)
    with allure.step(f"Generate tech-support and verify it holds the step-1 leaves and debug counters of {port_name}"):
        try:
            shown = nvl7_access_operational(Fae(port_name=port_name))
            techsupport.action_generate()
            techsupport.extract_techsupport_files(engine)
            fields = {leaf: 'phy_recovery_step_1_' + leaf.replace('-', '_') for leaf in consts.NVL7_ACCESS_LEAVES}
            configured = techsupport.get_db_dump_hash(engine, 'CONFIG_DB', f'.["IB_PORT|{sonic_name}"]')
            mismatched = {field: configured.get(field) for leaf, field in fields.items()
                          if configured.get(field, str(consts.NVL7_ACCESS_LEAF_DEFAULT)) !=
                          str(applied.get(leaf, consts.NVL7_ACCESS_LEAF_DEFAULT))}
            assert not mismatched, f"Configured step-1 leaves of {sonic_name} wrong in tech-support: {mismatched}"
            operational = techsupport.get_db_dump_hash(engine, 'STATE_DB', f'.["IB_PORT_TABLE|{sonic_name}"]')
            mismatched = {field: operational.get(field) for leaf, field in fields.items()
                          if field not in operational or (leaf in shown and operational[field] != str(shown[leaf]))}
            assert not mismatched, (
                f"Operational step-1 leaves of {sonic_name} missing or wrong in tech-support: {mismatched}, "
                f"shown {shown}"
            )
            counters = techsupport.get_db_dump_hash(
                engine, 'COUNTERS_DB', f'.["COUNTERS:" + .COUNTERS_PORT_NAME_MAP.value["{sonic_name}"]]')
            missing = [counter for counter in consts.NVL7_DEBUG_COUNTER_SAI_STATS if counter not in counters]
            assert not missing, f"Debug counters of {sonic_name} missing from tech-support: {missing}"
        finally:
            if techsupport.file_name:
                techsupport.cleanup(engine)
                techsupport.files.file_name[techsupport.file_name].action_delete()


def _assert_step_2_access_sets_rejected(engine, fae_port):
    consts = PhyRecoveryConsts
    rest = TestToolkit.tested_api == ApiType.OPENAPI
    error = consts.NVL7_ERR_REST_STEP_2_LEAF if rest else consts.NVL7_ERR_STEP_2_LEAF
    phy_recovery = fae_port.port.interface.link.phy_recovery
    with allure.step(f"Set of each step-1 access leaf under {consts.STEP_2} is rejected, nothing staged"):
        for leaf in consts.NVL7_ACCESS_LEAVES:
            set_phy_recovery_attribute(phy_recovery, [consts.STEP_2], leaf, 1,
                                       expected_str=error.format(leaf=leaf)).verify_result()
        staged = nvl7_staged_config(engine)
        assert staged in ('', '{}'), f"Rejected {consts.STEP_2} sets left staged config: {staged}"


def _wait_for_flaps(engine, marker, port_name, expected):
    def _check():
        flaps = nvl_port_flaps_since(engine, marker, port_name)
        assert flaps == expected, f"Expected {expected} oper up->down flap(s) on {port_name}, got {flaps}"

    retry_call(_check, exceptions=AssertionError, tries=12, delay=10)


def _assert_flap_only_on_changed_port(engine, marker, changed_name, unchanged_name):
    with allure.step(f"Exactly one flap on changed {changed_name}, none on unchanged {unchanged_name}"):
        _wait_for_flaps(engine, marker, changed_name, 1)
        flaps = nvl_port_flaps_since(engine, marker, unchanged_name)
        assert flaps == 0, f"Expected no oper up->down flap on unchanged {unchanged_name}, got {flaps}"


@pytest.mark.interfaces
@pytest.mark.simx
@pytest.mark.multiplanar
@pytest.mark.timeout(20 * MINUTE, func_only=True)
def test_nvl7_access_phy_recovery_bad_flow(engines, devices, random_api, nvl7_acp_phy_recovery):
    """
    @summary: Validate rejection of bad NVL7 access-port step-1 recovery values on one end of an acp pair
        (on simx the two ports are cabled to each other).
        1. Over-max, -1 and non-integer values are rejected at set time on each of the six leaves;
           nothing is staged.
        2. Record the baseline: health status, SAI/SDK dump files, swss/syncd start times and orchagent PIDs,
           and operational step-1 leaves.
        3. Apply each value the device rejects (over its cap or on an unsupported preset), one per apply:
           exactly one rejection warning, applied equals requested, admin state restored.
        4. The rejected values are not retried while idle for 60 seconds.
        5. Health status and dump files are unchanged; dockers are up and swss/syncd/orchagent did not restart.
        6. Operational step-1 leaves still equal the baseline.
        7. Unset phy-recovery: applied leaves are back to defaults and no rejection is logged.
    """
    consts = PhyRecoveryConsts
    engine = engines.dut
    system = System()
    port_name = nvl7_acp_phy_recovery[0]
    port = Fae(port_name=port_name)
    caps = devices.dut.phy_recovery_step1_caps
    rejected = [(leaf, cap + 1) for leaf, cap in caps.items()]
    rejected += [(leaf, 3) for leaf in consts.NVL7_ACCESS_LEAVES if leaf not in caps][:1]

    _assert_access_set_errors(engine, port, random_api)
    _skip_if_apply_time_rejection_untestable(caps)

    with allure.step("Record the baseline"):
        wait_for_ports_state(devices, [port_name], 'acp')
        operational = nvl7_access_operational(port)
        nvl7_assert_access_leaves_in_range(port_name, operational)
        health = get_health_status(system)
        dumps = _sai_dump_files(engine)
        starts = _swss_syncd_starts(engine)
        sonic_name = nvl_port_sonic_name(engine, port_name)

    for leaf, value in rejected:
        with allure.step(f"Apply rejected {leaf} {value}: one warning, applied, admin state restored"):
            admin_state = nvl7_link_admin_state(port)
            marker = mark_syslog(engine)
            nvl7_stage_and_apply_leaves(port, [(leaf, value)], subtree=consts.STEP_1)
            assert_one_log_line_since(engine, marker, consts.NVL7_SAI_REJECT_LOG.format(
                port=sonic_name, attr=consts.NVL7_ACCESS_LEAF_SAI_ATTR[leaf], value=value),
                f"rejection of {leaf} {value} on {sonic_name}")
            nvl7_verify_new_leaves(port, {leaf: value}, subtree=consts.STEP_1, applied=True)
            wait_for_ports_state(devices, [port_name], 'acp')
            assert nvl7_link_admin_state(port) == admin_state, f"{port_name} admin state not restored after apply"

    with allure.step("Rejected values are not retried while idle for 60 seconds"):
        marker = mark_syslog(engine)
        time.sleep(60)  # no observable event for "not retried": watch the log over a fixed window
        retried = nvl7_sai_rejects_since(engine, marker, sonic_name, [leaf for leaf, _ in rejected])
        assert retried == 0, f"{retried} rejection(s) re-logged on {sonic_name} while idle"

    with allure.step("Health status and dump files unchanged; swss, syncd and orchagent not restarted"):
        assert get_health_status(system) == health, f"Health status changed from {health}"
        devices.dut.verify_dockers(engine).verify_result()
        assert _swss_syncd_starts(engine) == starts, f"swss/syncd/orchagent restarted, baseline:\n{starts}"
        assert _sai_dump_files(engine) == dumps, "New SAI/SDK dump files after the rejections"

    with allure.step("Operational step-1 leaves still equal the baseline"):
        wait_for_ports_state(devices, [port_name], 'acp')
        nvl7_verify_new_leaves(port, operational, subtree=consts.STEP_1)

    with allure.step("Unset phy-recovery: applied leaves back to defaults, no new rejection"):
        marker = mark_syslog(engine)
        port.port.interface.link.phy_recovery.unset(apply=True, ask_for_confirmation=True).verify_result()
        nvl7_verify_new_leaves(port, consts.NVL7_ACCESS_LEAF_DEFAULTS, subtree=consts.STEP_1, applied=True)
        wait_for_ports_state(devices, [port_name], 'acp')
        rejects = nvl7_sai_rejects_since(engine, marker, sonic_name, consts.NVL7_ACCESS_LEAVES)
        assert rejects == 0, f"{rejects} rejection(s) logged on {sonic_name} by the unset"


def _skip_if_apply_time_rejection_untestable(caps):
    if not caps:
        pytest.skip("Device step-1 caps unknown (O-5): apply-time rejection not tested")
    if is_bug_active(5350786):
        pytest.skip("Bug 5350786: a rejected step-1 value takes the SAI-failure path, apply-time rejection not tested")


def _assert_access_set_errors(engine, fae_port, random_api):
    consts = PhyRecoveryConsts
    rest = random_api == ApiType.OPENAPI
    cases = []
    for leaf, maximum in consts.NVL7_ACCESS_LEAF_MAX.items():
        range_error = consts.NVL7_ERR_ACCESS_RANGE.format(leaf=leaf, max=maximum)
        cases += [
            (leaf, maximum + 1, consts.NVL7_ERR_REST_OVER_MAX.format(max=maximum) if rest else range_error),
            (leaf, -1, consts.NVL7_ERR_REST_UNDER_MIN if rest else range_error),
            (leaf, 'abc', consts.NVL7_ERR_REST_NOT_INTEGER if rest else consts.NVL7_ERR_NOT_INTEGER),
        ]
    phy_recovery = fae_port.port.interface.link.phy_recovery
    with allure.step(f"Out-of-range and non-integer sets are rejected on {fae_port.port.name}, nothing staged"):
        for leaf, value, expected_str in cases:
            with allure.independent_step(f"Set {leaf} {value} is rejected"):
                set_phy_recovery_attribute(phy_recovery, [consts.STEP_1], leaf, value,
                                           expected_str=expected_str).verify_result()
        staged = nvl7_staged_config(engine)
        assert staged in ('', '{}'), f"Rejected sets left staged config: {staged}"


def _sai_dump_files(engine):
    return engine.run_cmd(f"sudo ls -A {SAI_FAILURE_DUMP_DIR} {SAI_DFW_DUMP_DIR_GLOB}")


def _swss_syncd_starts(engine):
    starts = engine.run_cmd(
        "docker inspect -f '{{.Name}} {{.State.StartedAt}}' $(docker ps -q -f name=swss -f name=syncd);"
        " pgrep -ax orchagent")
    assert all(name in starts for name in ('swss', 'syncd', 'orchagent')), f"Unexpected docker/pgrep output: {starts}"
    return starts


def _assert_nvl7_leaves_pruned(fae_port, port_name):
    """Assert the 5 CPO leaves + counters debug are absent and a CPO-leaf set is rejected on a port."""
    _, missing = nvl7_new_leaves_present(fae_port)
    assert set(missing) == set(PhyRecoveryConsts.NVL7_NEW_LEAVES), (
        f"Expected all new leaves pruned on {port_name}, but missing only: {missing}"
    )
    counters_missing = nvl7_missing_debug_counters(fae_port)
    assert set(counters_missing) == set(PhyRecoveryConsts.NVL7_DEBUG_COUNTERS), (
        f"counters debug node (partially) present on {port_name}: "
        f"present={sorted(set(PhyRecoveryConsts.NVL7_DEBUG_COUNTERS) - set(counters_missing))}"
    )
    _assert_nvl7_pruned_set_rejected(fae_port)


def _assert_nvl7_access_leaves_pruned(fae_port):
    """Assert the 6 step-1 access leaves are absent and a set of one is rejected on a non-acp port."""
    consts = PhyRecoveryConsts
    _, missing = nvl7_new_leaves_present(fae_port, consts.NVL7_ACCESS_LEAVES, subtree=consts.STEP_1)
    assert set(missing) == set(consts.NVL7_ACCESS_LEAVES), (
        f"Expected all access leaves pruned on {fae_port.port.name}, but missing only: {missing}"
    )
    _assert_nvl7_pruned_set_rejected(fae_port, consts.ITERATIONS_PRESET3, 1, subtree=consts.STEP_1)


def _assert_nvl7_pruned_set_rejected(fae_port, leaf=PhyRecoveryConsts.RECOVERY_POLICY_CONFIG,
                                     value=PhyRecoveryConsts.RecoveryPolicyConfig.ROBUST_OPTIMIZED, subtree=None):
    with allure.step(f"Set of {leaf} is rejected (pruned) on {fae_port.port.name}"):
        set_phy_recovery_attribute(fae_port.port.interface.link.phy_recovery, [subtree or leaf], leaf, value,
                                   expected_str=PhyRecoveryConsts.NVL7_ERR_PRUNED_SET).verify_result()


# -------------------------- NVL7 orchestration helpers --------------------------


def _require_nvl7_trunk_fae_port(requested_state=NvosConsts.LINK_STATE_UP):
    """Select an NVL7 trunk (sw*) port as a Fae object, or pytest.skip if none are available."""
    fae_port = select_nvl7_trunk_fae_port(requested_state)
    if fae_port is None:
        pytest.skip("Skipping NVL7 trunk test - no trunk (sw*) ports found")
    return fae_port


def _nvl7_cleanup(selected_port):
    """Unset phy-recovery on a trunk port and wait for it to come back up (shared test cleanup)."""
    with allure.step("Cleanup - unset phy-recovery and wait for trunk up"):
        selected_port.port.interface.link.phy_recovery.unset(
            apply=True, ask_for_confirmation=True).verify_result()
        nvl7_wait_trunk_up(selected_port.port.name)


def _run_nvl7_phy_recovery_attributes(devices):
    """NVL7 attributes flow: validate defaults + new-leaf presence on a trunk port, then sweep."""
    selected_port = _require_nvl7_trunk_fae_port()
    try:
        with allure.step("Validate inherited NVL6 default attributes are present on the NVL7 trunk port"):
            output = nvl7_parse_phy_recovery(selected_port)
            _validate_default_values(output, devices.dut.default_phy_recovery_attributes)

        with allure.step("Validate the 5 new NVL7 CPO leaves show their literal default values"):
            # Per dev (2026-07-17): defaults are policy=fw-default, re-iteration=fw-default, timers=0,
            # echoed literally. Exact-verify includes presence (a missing leaf mismatches its default).
            nvl7_verify_new_leaves(selected_port, PhyRecoveryConsts.NVL7_NEW_LEAF_DEFAULTS)

        with allure.step("Enable recovery (recovery-status enabled) before the set/verify sweep"):
            nvl7_enable_recovery(selected_port)
            nvl7_wait_trunk_up(selected_port.port.name)

        _sweep_nvl7_new_leaves(selected_port)
    finally:
        _nvl7_cleanup(selected_port)


def _sweep_nvl7_new_leaves(selected_port):
    """
    Deterministic set-and-verify sweep of the 5 new leaves.

    Per dev (2026-07-17): NVL7 show echoes the CONFIGURED value LITERALLY (timer 0 -> 0, fw-default
    -> "fw-default"; there is NO FW-resolved display), so EVERY configured value round-trips exactly
    - including under preset policies. Each round sets all 5 leaves to different values (covering the
    enum values, timer boundaries 0/255, and fw-default) and asserts an exact match.
    """
    consts = PhyRecoveryConsts
    rounds = [
        {
            consts.RECOVERY_POLICY_CONFIG: consts.RecoveryPolicyConfig.EXPLICIT_CONFIG_CONTROLS,
            consts.RECOVERY_TX_RE_ITERATION: consts.RecoveryTxReIteration.ENABLED,
            consts.RECOVERY_TX_TOGGLE_DELAY: 1,
            consts.RECOVERY_TX_TOGGLE_TIME: 255,
            consts.RECOVERY_RX_ALGO_TIME: 128,
        },
        {
            consts.RECOVERY_POLICY_CONFIG: consts.RecoveryPolicyConfig.ROBUST_OPTIMIZED,
            consts.RECOVERY_TX_RE_ITERATION: consts.RecoveryTxReIteration.DISABLED,
            consts.RECOVERY_TX_TOGGLE_DELAY: 255,
            consts.RECOVERY_TX_TOGGLE_TIME: 1,
            consts.RECOVERY_RX_ALGO_TIME: 64,
        },
        {
            consts.RECOVERY_POLICY_CONFIG: consts.RecoveryPolicyConfig.GRADUAL_RECOVERY,
            consts.RECOVERY_TX_RE_ITERATION: consts.RecoveryTxReIteration.FW_DEFAULT,
            consts.RECOVERY_TX_TOGGLE_DELAY: 0,
            consts.RECOVERY_TX_TOGGLE_TIME: 100,
            consts.RECOVERY_RX_ALGO_TIME: 0,
        },
    ]
    for index, batch in enumerate(rounds, start=1):
        with allure.step(f"NVL7 new-leaf sweep round {index}/{len(rounds)}: set + exact-verify {batch}"):
            nvl7_stage_and_apply_leaves(selected_port, batch.items())
            nvl7_wait_trunk_up(selected_port.port.name)
            nvl7_verify_new_leaves(selected_port, batch)


def _nvl7_bulk_trunks_up(trunk_names):
    """
    Toggle the whole trunk range up in ONE ranged apply, then poll ALL ports oper-up with a
    single show per retry (no per-port sequential waits). Any phy-recovery apply admin-DOWNs
    every affected NVL7 port and they stay down - a bulk apply downs the whole fabric.
    """
    range_str = summarize_switch_ports(trunk_names)
    with allure.step(f"Toggle all {len(trunk_names)} trunk ports up (ranged) and wait for oper up"):
        Port(range_str).interface.link.state.set(
            op_param_name=NvosConsts.LINK_STATE_UP, apply=True, ask_for_confirmation=True
        ).verify_result()
        retry_call(validate_ports_state, fargs=[trunk_names, 'sw'], exceptions=Exception,
                   tries=NVL7_TRUNK_LINK_UP_TRIES, delay=NVL7_TRUNK_LINK_UP_DELAY, logger=logger)


def _run_nvl7_bulk_trunk_new_leaves(trunk_names):
    """
    Bulk-set recovery-policy-config + one numeric timer leaf on the whole trunk range in one apply.

    ``trunk_names`` is the (non-empty) dynamic sw* list from the caller.
    """
    consts = PhyRecoveryConsts
    all_ports = Fae(port_name=summarize_switch_ports(trunk_names))
    expected = {
        consts.RECOVERY_POLICY_CONFIG: consts.RecoveryPolicyConfig.EXPLICIT_CONFIG_CONTROLS,
        consts.RECOVERY_TX_TOGGLE_TIME: 100,
    }
    verify_ports = random.sample(trunk_names, min(3, len(trunk_names)))
    try:
        with allure.step(f"Bulk set {expected} on all trunk ports in one apply"):
            nvl7_stage_and_apply_leaves(all_ports, expected.items())
        # the bulk apply admin-DOWNed EVERY trunk port - restore the whole fabric, not a sample
        _nvl7_bulk_trunks_up(trunk_names)

        with allure.step(f"Verify the bulk config on {len(verify_ports)} random trunk ports"):
            for port_name in verify_ports:
                nvl7_verify_new_leaves(Fae(port_name=port_name), expected)
    finally:
        with allure.step("Bulk unset phy-recovery on all trunk ports"):
            all_ports.port.interface.link.phy_recovery.unset(
                apply=True, ask_for_confirmation=True
            ).verify_result()
        # the unset apply downs the fabric again - restore before leaving the test
        _nvl7_bulk_trunks_up(trunk_names)


def _run_nvl7_new_leaves_bad_flow(devices):
    """Reject bad values for the 5 new leaves on a trunk port (bad enum, 256, -1, abc)."""
    consts = PhyRecoveryConsts
    selected_port = _require_nvl7_trunk_fae_port(NvosConsts.LINK_STATE_ALL_TYPES)
    phy_recovery = selected_port.port.interface.link.phy_recovery

    with allure.independent_step(f"Reject bad enum for {consts.RECOVERY_POLICY_CONFIG}"):
        phy_recovery.set(
            consts.RECOVERY_POLICY_CONFIG, "bad-policy", expected_str=consts.NVL7_ERR_ENUM_INVALID
        ).verify_result()

    with allure.independent_step(f"Reject bad enum for {consts.RECOVERY_TX_RE_ITERATION}"):
        phy_recovery.set(
            consts.RECOVERY_TX_RE_ITERATION, "abc", expected_str=consts.NVL7_ERR_ENUM_INVALID
        ).verify_result()

    for leaf in consts.NVL7_TIMER_LEAVES:
        with allure.independent_step(f"Reject 256 (over max {consts.NVL7_TIMER_MAX}) for {leaf}"):
            phy_recovery.set(leaf, 256, expected_str=consts.NVL7_ERR_NUMERIC_OVER_MAX).verify_result()
        with allure.independent_step(f"Reject -1 (negative) for {leaf}"):
            phy_recovery.set(leaf, -1, expected_str=consts.NVL7_ERR_NUMERIC_NEGATIVE).verify_result()
        with allure.independent_step(f"Reject 'abc' (non-numeric) for {leaf}"):
            phy_recovery.set(leaf, "abc", expected_str=consts.NVL7_ERR_NUMERIC_NON_NUMERIC).verify_result()


def _nvl7_non_nvl_link_ports(devices):
    """eth0, lo and one internal fnm port: no phy-recovery node at all."""
    ports = [name for name in ('eth0', 'lo') if name in devices.dut.network_ports]
    if devices.dut.nvl_internal_fnm_ports:
        ports.append(random.choice(devices.dut.nvl_internal_fnm_ports))
    return ports


def _verify_nvl7_debug_counters_default(selected_port, devices):
    """Verify the debug step-counters match the device default (all zero) - device is source of truth."""
    expected = devices.dut.default_phy_recovery_debug_counters

    def _check():
        counters = nvl7_read_debug_counters(selected_port)
        assert counters == expected, f"Expected default debug counters {expected}, got {counters}"

    retry_call(_check, exceptions=AssertionError, tries=5, delay=5)


def _verify_nvl7_step_counters_incremented(selected_port, before):
    def _check():
        after = nvl7_read_debug_counters(selected_port)
        consts = PhyRecoveryConsts
        step_count_increased = (
            after[consts.TOTAL_STEP_1_COUNT] > before[consts.TOTAL_STEP_1_COUNT] or
            after[consts.TOTAL_STEP_2_COUNT] > before[consts.TOTAL_STEP_2_COUNT]
        )
        assert step_count_increased, f"No step count increment: before={before} after={after}"
        time_in_last_positive = (
            after[consts.TIME_IN_LAST_STEP_1] > 0 or after[consts.TIME_IN_LAST_STEP_2] > 0
        )
        assert time_in_last_positive, f"time-in-last-step still 0 after recovery: {after}"

    retry_call(_check, exceptions=AssertionError, tries=8, delay=10)
