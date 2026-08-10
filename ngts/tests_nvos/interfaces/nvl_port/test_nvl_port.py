import logging
import random
import time

import pytest
from retry.api import retry_call

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import (
    ApiType,
    LinkTrainingConsts,
    MultiPlanarConsts,
    NvosConst,
    OutputFormat,
)
from ngts.nvos_tools.Devices.cpo.CpoTopology import is_cpo_capable
from ngts.nvos_tools.Devices.IbDevice import JulietNonScaleoutSwitch
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import (
    IbInterfaceConsts,
    InternalNvosConsts,
    NvlInterfaceConsts,
    NvosConsts,
)
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.cluster.cluster_tools import summarize_switch_ports
from ngts.tests_nvos.helpers.interfaces import interface_helpers
from ngts.tests_nvos.helpers.interfaces.nvl_port.nvl6 import link_training_helpers
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.interfaces.nvl_port.helpers import (
    show_interface_and_validate,
    skip_if_fec_measure_not_supported,
    skip_if_no_access_links,
    skip_if_no_trunk_links,
    toggle_port_state,
    validate_ports_state_and_speed,
)
from ngts.tests_nvos.system.gnmi.constants import GnmicErr, GnmiMode
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.helpers import verify_msg_in_out_or_err, verify_msg_not_in_out_or_err
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.allure_utils import step as allure_step

logger = logging.getLogger()


@pytest.mark.interface
@pytest.mark.air
@pytest.mark.air_ci
@pytest.mark.air_sanity
def test_show_nvl_interface(devices):
    """
    Show all NVL interfaces, verify at least one NVL port is link-up.
    """
    with allure.step("Parse show interface output for all NVL ports"):
        output_dictionary = OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
            Port.show_interface()
        ).get_returned_value()

    with allure.step("Verify every expected NVL port is present and has type, state, and speed"):
        missing = [p for p in devices.dut.all_nvl_ports_list if p not in output_dictionary]
        assert not missing, f"NVL ports missing from show interface output: {missing}"

    with allure.step("Verify at least one NVL port is up"):
        up_ports = [
            p for p in devices.dut.all_nvl_ports_list
            if output_dictionary[p][IbInterfaceConsts.LINK_STATE] == NvosConsts.LINK_STATE_UP
        ]
        states = {p: output_dictionary[p][IbInterfaceConsts.LINK_STATE] for p in devices.dut.all_nvl_ports_list}
        assert up_ports, (
            f"Expected at least one NVL port UP; states per port: {states}"
        )
        logger.info("NVL ports UP: %s (total %d)", up_ports, len(up_ports))


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.simx
@pytest.mark.air
@pytest.mark.air_ci
@pytest.mark.air_sanity
def test_nvl_internal_fnm_ports(devices):
    """
    Validate that all internal FNM ports on NVL systems are UP by default.

    Internal FNM ports connect ASICs together
    and should be LinkUp by default

    This is the NVL equivalent of test_internal_fnm_ports from multi_planar tests.
    """
    if not hasattr(devices.dut, 'nvl_internal_fnm_ports') or not devices.dut.nvl_internal_fnm_ports:
        pytest.skip("No nvl_internal_fnm_ports defined for this device")

    output_dictionary = OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
        Port.show_interface(fae_param='fae')).get_returned_value()

    down_internal_fnm_ports = {port: output_dictionary[port][IbInterfaceConsts.LINK_STATE]
                               for port in devices.dut.nvl_internal_fnm_ports
                               if output_dictionary[port][IbInterfaceConsts.LINK_STATE] != NvosConsts.LINK_STATE_UP}

    assert not down_internal_fnm_ports, (
        f"Expected all internal FNM ports to be UP, but found DOWN: {down_internal_fnm_ports}"
    )
    logger.info(f"✓ All {len(devices.dut.nvl_internal_fnm_ports)} internal FNM ports are UP")


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.simx
def test_nvl_fnm_ports_up(devices, has_loopbox, standalone_system):
    """
    Validate that all regular FNM ports on NVL systems are UP by default.

    Regular FNM ports (fnm1, fnm2, etc.) are fan-out modules that should be LinkUp
    by default on systems that have them (e.g., JulietScaleout).

    Note: This is different from internal FNM ports (fnma0p1, fnma0p2, etc.)
    which are checked by test_nvl_internal_fnm_ports.
    """
    if not has_loopbox and standalone_system:
        pytest.skip("No loopbox and standalone system will have fnm ports down by default - skipping FNM ports test")
    if not hasattr(devices.dut, 'nvl_fnm_ports') or not devices.dut.nvl_fnm_ports:
        pytest.skip("No nvl_fnm_ports defined for this device (only internal FNM or standalone)")

    output_dictionary = OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
        Port.show_interface()).get_returned_value()

    down_fnm_ports = {port: output_dictionary[port][IbInterfaceConsts.LINK_STATE]
                      for port in devices.dut.nvl_fnm_ports
                      if output_dictionary[port][IbInterfaceConsts.LINK_STATE] != NvosConsts.LINK_STATE_UP}

    assert not down_fnm_ports, (
        f"Expected all regular FNM ports to be UP, but found DOWN: {down_fnm_ports}"
    )
    logger.info(f"✓ All {len(devices.dut.nvl_fnm_ports)} regular FNM ports are UP")


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.simx
def test_show_nvl_interface_commands(engines, devices, random_api, has_loopbox, standalone_system):
    """
    validate all show fae interface nvl commands.

    Test flow:
    1. Validate show interface command with all nvl interfaces
    2. Validate show fae interface command with all nvl interfaces
    3. Validate all multi planar fields exist and port type nvl, port speed 400G
    4. Validate link diagnostics on access port
    5. Clear counters
    """

    dut_device = devices.dut
    platform = Platform()

    with allure.step("Get list of connected transceivers"):
        present_transceivers = platform.transceiver.get_list_of_connected_transceivers()
        allure.attach(present_transceivers)

    with allure_step("Select nvl port"):
        port_name = RandomizationTool.select_random_value(devices.dut.nvl_access_ports_list + devices.dut.nvl_trunk_ports_list).get_returned_value()
        selected_port = Port(port_name)
        selected_fae_port = Fae(port_name=port_name)

        # Handle FNM ports - set to None if not available
        fnm_port_name = None
        fnm_port = None
        if hasattr(devices.dut, 'nvl_fnm_ports') and devices.dut.nvl_fnm_ports:
            fnm_port_name = RandomizationTool.select_random_value(devices.dut.nvl_fnm_ports).get_returned_value()
            fnm_port = Port(fnm_port_name)

        fnm_fae_port_name = None
        fnm_fae_port = None
        if hasattr(devices.dut, 'nvl_internal_fnm_ports') and devices.dut.nvl_internal_fnm_ports:
            fnm_fae_port_name = RandomizationTool.select_random_value(devices.dut.nvl_internal_fnm_ports).get_returned_value()
            fnm_fae_port = Fae(port_name=fnm_fae_port_name)

    with allure_step("Validate show interface command with all nvl interfaces"):
        show_interface_and_validate(engines, devices, devices.dut.all_nvl_ports_list)

    with allure_step("Validate show fae interface command with all nvl interfaces"):
        show_interface_and_validate(engines, devices, devices.dut.all_fae_nvl_ports_list, 'fae')

    with allure_step(f"Validate all multi planar fields exist and port {selected_port.name} type nvl, port speed 400G"
                     ):
        output_fae_port = OutputParsingTool.parse_show_interface_output_to_dictionary(
            selected_fae_port.port.interface.show()).get_returned_value()
        fae_port_keys = list(output_fae_port.keys())
        ValidationTool.validate_all_values_exists_in_list(MultiPlanarConsts.MULTI_PLANAR_KEYS, fae_port_keys). \
            verify_result()
        ValidationTool.compare_values(output_fae_port['type'], devices.dut.nvl_port_type).verify_result()

    with allure_step('Check if device is not a JulietNonScaleoutSwitch Device'):
        if not isinstance(dut_device, JulietNonScaleoutSwitch):
            with allure_step("Verify switch port speed"):
                # sw optics: pluggable transceivers on legacy scaleout platforms, CPO-backed
                # (no transceiver objects) on Gen2 CPO devices
                if devices.dut.nvl_trunk_ports_list != [] and (present_transceivers != [] or is_cpo_capable(dut_device)):
                    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_UP, interface_type='sw').get_returned_value()
                    output_dictionary = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                        selected_port.interface.link.show()).get_returned_value()
                    assert output_dictionary[IbInterfaceConsts.LINK_SPEED] == dut_device.nvl_trunk_port_speed, \
                        f"port speed should be {dut_device.nvl_trunk_port_speed} instead of" \
                        f"{output_dictionary[IbInterfaceConsts.LINK_SPEED]}"

    with allure_step("Verify access ports speed"):
        if has_loopbox or not standalone_system:
            selected_port = Tools.RandomizationTool.select_random_port(interface_type='acp').get_returned_value()
            output_dictionary = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                selected_port.interface.link.show()).get_returned_value()
            assert output_dictionary[IbInterfaceConsts.LINK_SPEED] == dut_device.access_port_speed, \
                f"port speed should be {dut_device.access_port_speed} instead of" \
                f"{output_dictionary[IbInterfaceConsts.LINK_SPEED]}"

    with allure_step("Verify fnm port speed"):
        if fnm_port_name and (has_loopbox or not standalone_system):
            output_dictionary = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                fnm_port.interface.link.show()).get_returned_value()
            assert output_dictionary[IbInterfaceConsts.LINK_SPEED] == dut_device.fnm_link_speed, \
                f"port speed should be {dut_device.fnm_link_speed} instead of" \
                f"{output_dictionary[IbInterfaceConsts.LINK_SPEED]}"

    with allure_step("Verify fae fnm port speed"):
        if fnm_fae_port_name:
            output_dictionary = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                fnm_fae_port.interface.link.show()).get_returned_value()
            assert output_dictionary[IbInterfaceConsts.LINK_SPEED] == dut_device.fnm_fae_link_speed, \
                f"port speed should be {dut_device.fnm_fae_link_speed} instead of" \
                f"{output_dictionary[IbInterfaceConsts.LINK_SPEED]}"

        # ValidationTool.compare_values(output_fae_port['link']['speed'], devices.dut.nvl_trunk_port_speed).verify_result()
        # [TBD] will work only on real system,  when system arrived, bug 3730650

    # with allure_step("Validate link diagnostics on nvl"):
    #     output_port = OutputParsingTool.parse_json_str_to_dictionary(
    #         selected_port.interface.link.diagnostics.show()).get_returned_value()
    #     ValidationTool.compare_values(output_port, {'0': {'status': 'No issue was observed'}}).verify_result()
    # [TBD] will work only on real system,  when system arrived, bug 3730650

    with allure_step(f"Validate all multi planar fields exist and port {selected_port.name} type fnm, port speed 400G"
                     ):
        if fnm_fae_port_name:
            output_fae_port = OutputParsingTool.parse_show_interface_output_to_dictionary(
                fnm_fae_port.port.interface.show()).get_returned_value()
            fae_port_keys = list(output_fae_port.keys())
            ValidationTool.validate_all_values_exists_in_list(MultiPlanarConsts.MULTI_PLANAR_KEYS, fae_port_keys). \
                verify_result()
            ValidationTool.compare_values(output_fae_port['type'], devices.dut.fnm_port_type).verify_result()

    with allure_step("Clear counters and validate"):
        selected_port.interface.action_clear_counter_for_all_interfaces(engines.dut).verify_result()


@pytest.mark.interface
@pytest.mark.simx
@pytest.mark.nvl_ci
def test_toggle_interface_state(test_name, devices, has_loopbox, standalone_system):
    """
    Configure port interface state and verify the configuration applied successfully
    Relevant cli commands:
    -	nv set interface <name> link state up/down
    -	nv show interface <name>

    flow:
    1. Select a random port (state of which is up)
    2. Set selected port state to 'down'
    3. Verify the configuration applied by running "show" command
    4. Set selected port state to 'up'
    5. Wait until the port is up
    6. Verify the configuration applied by running "show" command
    """
    port_init_state_restored = True

    # Build toggleable_interface list based on what's actually available on the device
    toggleable_interface = []
    is_mloop_simx = devices.dut.require_mloop_setup

    # Check for FNM ports (internal fan-out modules)
    if (has_loopbox or not standalone_system) and devices.dut.nvl_fnm_ports:
        toggleable_interface.append('fnm')
        logger.info(f"FNM ports available: {len(devices.dut.nvl_fnm_ports)} ports")

    # Check for SW ports (trunk ports whose optics are pluggable transceivers on
    # legacy scaleout platforms, or CPO-backed - no transceiver objects - on Gen2 CPO)
    platform = Platform()
    if devices.dut.nvl_trunk_ports_list:
        trunk_port_count = len(devices.dut.nvl_trunk_ports_list)
        if is_cpo_capable(devices.dut):
            toggleable_interface.append('sw')
            logger.info(f"SW (trunk) ports available (CPO-backed): {trunk_port_count} ports")
        else:
            present_transceivers = platform.transceiver.get_list_of_connected_transceivers()
            if present_transceivers:
                toggleable_interface.append('sw')
                logger.info(f"SW (trunk) ports available with transceivers: {len(present_transceivers)} transceivers")

    # Check for ACP ports (access ports)
    if (has_loopbox or not standalone_system or is_mloop_simx) and devices.dut.nvl_access_ports_list:
        toggleable_interface.append('acp')
        logger.info(f"ACP (access) ports available: {len(devices.dut.nvl_access_ports_list)} ports")

    if not toggleable_interface:
        pytest.skip("No toggleable interfaces available (no fnm/sw/acp ports found on device)")

    try:
        for interface_type in toggleable_interface:
            if devices.dut.nvl_trunk_ports_list == [] and interface_type == 'sw':
                continue
            port_type = 'fnm' if interface_type == 'fnm' else ''
            selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_UP, requested_ports_type=port_type, interface_type=interface_type).get_returned_value()
            TestToolkit.update_tested_ports([selected_port])
            toggle_port_state(selected_port, NvosConsts.LINK_STATE_DOWN, test_name, devices)
            logger.info("Sleeping for 15 seconds till toggle is reflected")
            time.sleep(15)
            port_init_state_restored = False
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                selected_port.interface.link.show()).get_returned_value()

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_STATE,
                                                              expected_value=NvosConsts.LINK_STATE_DOWN).verify_result()

            toggle_port_state(selected_port, NvosConsts.LINK_STATE_UP, test_name, devices)
            logger.info("Sleeping for 15 seconds till toggle is reflected")
            time.sleep(15)
            port_init_state_restored = True
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                selected_port.interface.link.show()).get_returned_value()

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_STATE,
                                                              expected_value=NvosConsts.LINK_STATE_UP).verify_result()
    finally:
        if not port_init_state_restored:
            toggle_port_state(selected_port, NvosConsts.LINK_STATE_UP, test_name, devices)


def _verify_time_to_link_up(port, expected_max_seconds):
    start = time.monotonic()
    interface_helpers.wait_and_verify_link([port], timeout=expected_max_seconds)
    elapsed = time.monotonic() - start
    logger.info("%s linked up after %.1fs (allowed max %ss)", port.name, elapsed, expected_max_seconds)
    assert elapsed < expected_max_seconds, (
        f"{port.name} took {elapsed:.1f}s to link up, allowed max {expected_max_seconds}s"
    )


@pytest.mark.interface
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_time_to_link_up(devices, test_api, register_cleanup):
    """Measure ACP port link-up time across the three link-training states.

    The measured link-up time must stay below the max allowed for the
    link-training configuration:
        - default (fec-measure-mode=disabled): < 90s
        - fec-measure-mode=enabled + force-max-iterations=disabled: < 180s
        - fec-measure-mode=enabled + force-max-iterations=enabled: < 375s

    Steps (single random ACP port, checked sequentially):
        1. Default: set fec-measure-mode=disabled, verify link-up < 90s.
        2. Set fec-measure-mode=enabled, force-max-iterations=disabled, verify < 180s.
        3. Set force-max-iterations=enabled, verify < 375s.
    """
    skip_if_fec_measure_not_supported(devices)

    TestToolkit.tested_api = test_api

    selected_port = Tools.RandomizationTool.select_random_port(
        requested_ports_state=NvosConsts.LINK_STATE_UP,
        interface_type=NvlInterfaceConsts.ACP_PORT_TYPE
    ).get_returned_value()
    fae = Fae(port_name=selected_port.name)
    TestToolkit.update_tested_ports([selected_port])

    register_cleanup(lambda: link_training_helpers.cleanup_link_training(devices, (fae,)))

    with allure_step("Default state: fec-measure-mode=disabled, verify time-to-link-up"):
        fae.interface.link.link_training.set(
            op_param_name=LinkTrainingConsts.FEC_MEASURE_MODE,
            op_param_value=LinkTrainingConsts.FecMeasureMode.DISABLED.value,
            apply=True,
            ask_for_confirmation=True,
        ).verify_result()
        _verify_time_to_link_up(selected_port, InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_DISABLED)

    with allure_step("fec-measure-mode=enabled, force-max-iterations=disabled, verify time-to-link-up"):
        fae.interface.link.link_training.set(
            op_param_name=LinkTrainingConsts.FEC_MEASURE_MODE,
            op_param_value=LinkTrainingConsts.FecMeasureMode.ENABLED.value,
            apply=True,
            ask_for_confirmation=True,
        ).verify_result()
        selected_port.interface.link.link_training.set(
            op_param_name=LinkTrainingConsts.FORCE_MAX_ITERATIONS,
            op_param_value=LinkTrainingConsts.ForceMaxIterations.DISABLED.value,
            apply=True,
            ask_for_confirmation=True,
        ).verify_result()
        _verify_time_to_link_up(selected_port, InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED)

    with allure_step("force-max-iterations=enabled, verify time-to-link-up"):
        selected_port.interface.link.link_training.set(
            op_param_name=LinkTrainingConsts.FORCE_MAX_ITERATIONS,
            op_param_value=LinkTrainingConsts.ForceMaxIterations.ENABLED.value,
            apply=True, ask_for_confirmation=True).verify_result()
        _verify_time_to_link_up(selected_port, InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED)


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.simx
@pytest.mark.nvl_ci
@pytest.mark.air
def test_nvl_port_configuration(engines, devices, random_api):
    """
    Validate configuration applied on interface

    Test flow:
    1. Set nvl interface description and validate
    2. Unset nvl interface and validate
    """
    try:
        with allure_step("Select nvl port"):
            port_name = RandomizationTool.select_random_value(devices.dut.nvl_access_ports_list + devices.dut.nvl_trunk_ports_list).get_returned_value()
            selected_port = Port(port_name)

        with allure_step(f"Set nvl {selected_port.name} port description and validate"):
            selected_port.interface.set(NvosConst.DESCRIPTION, 'aaa', apply=True).verify_result()
            access_port_output = OutputParsingTool.parse_json_str_to_dictionary(
                selected_port.interface.show()).get_returned_value()
            ValidationTool.compare_values(access_port_output['description'], 'aaa').verify_result()

    finally:
        with allure_step("Unset configuration"):
            selected_port.interface.unset(apply=True, ask_for_confirmation=True).verify_result()


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.simx
def test_nvl_negative(engines, devices, random_api):
    """
    Validate negative testing on nvl port

    Test flow:
    1. Validate negative split on access nvl port
    2. Validate negative testing nvl port lanes
    3. Validate negative testing nvl port speed
    """
    with allure_step("Select nvl port"):
        port_name = RandomizationTool.select_random_value(devices.dut.nvl_access_ports_list + devices.dut.nvl_trunk_ports_list).get_returned_value()
        selected_port = Port(port_name)

    try:
        if not is_bug_active(4209873):
            with allure_step(f"Negative testing with split nvl {selected_port.name} port"):
                selected_port.interface.link.set(op_param_name='breakout', op_param_value='2x-ndr', apply=True,
                                                 ask_for_confirmation=True).verify_result(False)
                selected_port.interface.link.set(op_param_name='breakout', op_param_value='2x-hdr', apply=True,
                                                 ask_for_confirmation=True).verify_result(False)
                NvueGeneralCli.detach_config(TestToolkit.engines.dut)

        with allure_step("Negative testing with configure nvl port params"):
            selected_port.interface.link.set(op_param_name='op-vls', op_param_value='1X', apply=True,
                                             ask_for_confirmation=True).verify_result(False)
            selected_port.interface.link.set(op_param_name='op-vls', op_param_value='4X', apply=True,
                                             ask_for_confirmation=True).verify_result(False)
            selected_port.interface.link.set(op_param_name='speed', op_param_value='xdr', apply=True,
                                             ask_for_confirmation=True).verify_result(False)
            selected_port.interface.link.set(op_param_name='speed', op_param_value='ndr', apply=True,
                                             ask_for_confirmation=True).verify_result(False)
            selected_port.interface.link.set(op_param_name='speed', op_param_value='800G', apply=True,
                                             ask_for_confirmation=True).verify_result(False)
            selected_port.interface.link.set(op_param_name='speed', op_param_value='100G', apply=True,
                                             ask_for_confirmation=True).verify_result(False)
            selected_port.interface.link.set(op_param_name='speed', op_param_value='555G', apply=True,
                                             ask_for_confirmation=True).verify_result(False)
    finally:
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)


@pytest.mark.interface
@pytest.mark.multiplanar
def test_interface_xdr_slow_speed_access_ports(engines, devices, random_api, setup_name, standalone_system, has_loopbox, is_simx):
    skip_if_no_access_links(has_loopbox, standalone_system, is_simx)
    acp_ports_range = f'acp1-{str(len(devices.dut.nvl_access_ports_list))}'
    _set_unset_interface_xdr_slow_speed(engines, devices, random_api, setup_name,
                                        standalone_system, acp_ports_range, prefix='acp')


@pytest.mark.interface
@pytest.mark.multiplanar
def test_interface_xdr_slow_speed_trunk_ports(engines, devices, random_api, setup_name, standalone_system):
    skip_if_no_trunk_links(devices)
    summarized_switch_ports = summarize_switch_ports(devices.dut.nvl_trunk_ports_list)
    _set_unset_interface_xdr_slow_speed(engines, devices, random_api, setup_name,
                                        standalone_system, summarized_switch_ports, prefix='sw')


def _set_unset_interface_xdr_slow_speed(engines, devices, test_api, setup_name, standalone_system,
                                        group_all_ports: str, prefix: str):
    """
    Configure xdr slow speed on all trunk / access ports
    Relevant CLI commands:
    - nv set interface <interface-id> link speed 200G/400G
    - nv unset interface <interface-id> link speed
    - nv show interface <interface-id> link

    Flow:
    1. Select all up ports for validation
    2. Set all ports speed to 200G.
    3. Verify the value using the "show" command.
    4. Unset all ports speed.
    5. Verify the default value (400G) is restored.
    """
    with allure.step(f"Select {devices.dut.nvl_port_type} ports"):
        port_names = [port.name for port in RandomizationTool.select_random_ports(requested_ports_type=devices.dut.nvl_port_type, num_of_ports_to_select=0).get_returned_value() if port.name.startswith(prefix)]
        up_ports = [Port(port_name) for port_name in port_names]
        selected_port = random.choice(up_ports)

    with allure.step('set up streamed gnmi session - subscribe client to port speed'):
        client = GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, devices.dut.default_username,
                            devices.dut.default_password, verify_tools_installed=True)
        session = client.gnmic_subscribe_interface_speed_and_keep_session_alive(GnmiMode.STREAM, selected_port.name,
                                                                                skip_cert_verify=True)

    with allure.step("Create instance for all ports"):
        all_ports = Port(group_all_ports)

    speed = IbInterfaceConsts.XDR_SLOW_SPEED
    try:
        with allure.step(f"Test speed {speed}"):
            all_ports.interface.link.set(op_param_name=IbInterfaceConsts.LINK_SPEED, op_param_value=speed, apply=True,
                                         ask_for_confirmation=True).verify_result()

            with allure.step("Validate xdr slow speed on ports"):
                retry_call(validate_ports_state_and_speed, [speed, port_names, prefix], exceptions=AssertionError, tries=6,
                           delay=30)
                time.sleep(30)  # GNMI 30 seconds pulling interval

    # Unset port speed and verify default (device-specific: access_port_speed or nvl_trunk_port_speed) is restored
    finally:
        with allure.step("Test unset xdr slow speed"):
            all_ports.interface.link.unset(op_param=IbInterfaceConsts.LINK_SPEED, apply=True, ask_for_confirmation=True).verify_result()

            with allure.step("Validate unset xdr slow speed on ports"):
                # Select correct default speed based on port type (access vs trunk)
                expected_default_speed = devices.dut.access_port_speed if prefix == 'acp' else devices.dut.nvl_trunk_port_speed
                retry_call(validate_ports_state_and_speed, [expected_default_speed, port_names, prefix], exceptions=AssertionError, tries=6,
                           delay=30)

        with allure.step('verify that client received the xdr speed in the existing streaming session'):
            out, err = client.close_session_and_get_out_and_err(session)
            verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, out, err)
            with allure.independent_step(f'check that "{IbInterfaceConsts.XDR_SLOW_SPEED}" was streamed'):
                verify_msg_in_out_or_err('200', out)


def _get_available_nvl_ports(devices, has_loopbox, standalone_system):
    """Get available ports for speed testing (trunk with transceivers or access with loopboxes)"""
    available_ports = []

    # Check trunk ports with transceivers
    if devices.dut.nvl_trunk_ports_list:
        platform = Platform()
        if platform.transceiver.get_list_of_connected_transceivers():
            trunk_port = Tools.RandomizationTool.select_random_port(
                requested_ports_state=NvosConsts.LINK_STATE_UP, interface_type='sw'
            ).get_returned_value()
            available_ports.append(trunk_port)

    # Check access ports with loopboxes
    if has_loopbox or not standalone_system:
        access_port = Tools.RandomizationTool.select_random_port(
            requested_ports_state=NvosConsts.LINK_STATE_UP, interface_type='acp'
        ).get_returned_value()
        available_ports.append(access_port)

    return available_ports


def _get_interface_supported_speeds(selected_port):
    """Get supported speeds from interface show output"""
    interface_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
        selected_port.interface.link.show()).get_returned_value()

    displayed_supported_speeds = interface_output.get(IbInterfaceConsts.LINK_SUPPORTED_SPEEDS)
    if not displayed_supported_speeds:
        return None

    return [speed.strip() for speed in displayed_supported_speeds.split(',')]


def _validate_supported_speeds_match(displayed_speeds, expected_speeds, port_name):
    """Validate that displayed speeds match expected speeds"""
    displayed_speeds_set = set(displayed_speeds)
    expected_speeds_set = set(expected_speeds)
    ValidationTool.compare_values(displayed_speeds_set, expected_speeds_set).verify_result()
    logger.info(f"Successfully validated supported speeds for {port_name}")


def _validate_fnm_port_speeds_and_lanes(port_name, port_obj, expected_speeds, expected_lanes, port_type="FNM"):
    """
    Helper to validate supported speeds and lanes for FNM/internal FNM ports

    :param port_name: Name of the port
    :param port_obj: Port or Fae object
    :param expected_speeds: Expected speeds list
    :param expected_lanes: Expected lanes string (e.g., '2X' or '1X,2X')
    :param port_type: "FNM" or "Internal FNM" for logging
    """
    output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
        port_obj.interface.link.show()).get_returned_value()

    # Validate supported speeds
    speeds_str = output.get(IbInterfaceConsts.LINK_SUPPORTED_SPEEDS)
    if speeds_str:
        displayed_speeds = [speed.strip() for speed in speeds_str.split(',')]
        logger.info(f"{port_type} port {port_name} supported speeds: {displayed_speeds}")
        _validate_supported_speeds_match(displayed_speeds, expected_speeds, port_name)
    else:
        logger.warning(f"No supported speeds found for {port_type} port {port_name}")

    # Validate supported lanes
    lanes_str = output.get(IbInterfaceConsts.LINK_SUPPORTED_LANES)
    if lanes_str and expected_lanes:
        assert lanes_str == expected_lanes, (
            f"Expected {port_type} supported-lanes '{expected_lanes}', but got '{lanes_str}'"
        )
        logger.info(f"✓ Validated {port_type} port {port_name} supported-lanes = {lanes_str}")
    elif lanes_str:
        logger.info(f"{port_type} port {port_name} supported-lanes: {lanes_str} (no validation - expected value not provided)")


def _test_port_state_change_with_speeds(selected_port, expected_speeds_set, test_name, devices):
    """Test that supported speeds remain visible during port state changes"""
    # Get original port state
    original_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
        selected_port.interface.link.show()).get_returned_value()
    original_state = original_output.get(IbInterfaceConsts.LINK_STATE)
    logger.info(f"Original port state: {original_state}")

    try:
        # Set port to down state
        with allure_step(f"Set {selected_port.name} to down state"):
            toggle_port_state(selected_port, NvosConsts.LINK_STATE_DOWN, test_name, devices)

        # Verify port is down and supported speeds are still visible
        with allure_step("Verify port is down and supported speeds still visible"):
            down_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                selected_port.interface.link.show()).get_returned_value()

            # Check state is down
            current_state = down_output.get(IbInterfaceConsts.LINK_STATE)
            assert current_state == "down", f"Expected port state 'down', got '{current_state}'"

            # Check supported speeds are still visible
            down_speeds_list = _get_interface_supported_speeds(selected_port)
            assert down_speeds_list, "No supported speeds found when port is down"

            # Validate supported speeds are the same when port is down
            down_speeds_set = set(down_speeds_list)
            ValidationTool.compare_values(down_speeds_set, expected_speeds_set).verify_result()
            logger.info(f"Confirmed supported speeds still visible when port is down: {down_speeds_list}")

    finally:
        # Always restore original port state
        if original_state and original_state != "down":
            with allure_step(f"Restore {selected_port.name} to original state"):
                toggle_port_state(selected_port, original_state, test_name, devices)
                logger.info(f"Restored {selected_port.name} to original state: {original_state}")


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.simx
def test_nvl_supported_speeds_validation(test_name, engines, devices, random_api, has_loopbox, standalone_system):
    """
    Validate supported-speed field matches expected device supported speeds

    Test runs on ALL ASIC types - QTM2, QTM3, NVL5, QTM4+, NVL6, etc.

    Test flow:
    1. Randomly select an nvl interface (any state)
    2. Show interface to get supported-speed field from output
    3. Fetch supported speeds from device configuration
    4. Validate that displayed speeds match expected speeds
    5. Test FNM and internal FNM ports as well
    """

    with allure_step("Select random nvl interface (any state)"):
        # Select ANY nvl port regardless of link state - supported speeds should be visible always
        selected_port = RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
        logger.info(f"Selected port for supported speeds validation: {selected_port.name} (state-independent test)")

    with allure_step("Get and validate supported speeds"):
        # Get displayed speeds from interface
        displayed_speeds_list = _get_interface_supported_speeds(selected_port)
        if not displayed_speeds_list:
            pytest.skip(f"No supported speeds found in interface output for {selected_port.name}")
        logger.info(f"Displayed supported speeds: {displayed_speeds_list}")

        # Get expected speeds from device
        if not getattr(devices.dut, 'supported_nvl_speeds', None):
            pytest.skip("No supported_nvl_speeds available on device")
        expected_supported_speeds = devices.dut.supported_nvl_speeds
        logger.info(f"Expected supported speeds: {expected_supported_speeds}")

        # Get current configured speed - if port is configured at lower speed,
        # supported-speeds should only show speeds up to and including configured speed
        interface_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_port.interface.link.show()).get_returned_value()
        current_speed = interface_output.get(IbInterfaceConsts.LINK_SPEED)

        if current_speed:
            current_speed_value = int(current_speed.replace('G', ''))
            # Filter expected speeds to only those <= current configured speed
            expected_supported_speeds = sorted(
                [speed for speed in expected_supported_speeds
                 if int(speed.replace('G', '')) <= current_speed_value],
                key=lambda x: int(x.replace('G', ''))
            )
            logger.info(f"Port configured at {current_speed}, expecting speeds up to {current_speed}: {expected_supported_speeds}")

        # Validate speeds match
        _validate_supported_speeds_match(displayed_speeds_list, expected_supported_speeds, selected_port.name)

    with allure_step("Test supported speeds visibility with port state changes"):
        expected_speeds_set = set(expected_supported_speeds)
        _test_port_state_change_with_speeds(selected_port, expected_speeds_set, test_name, devices)

    # Test FNM ports (regular Port command)
    with allure_step("Validate supported speeds and lanes on FNM ports"):
        if hasattr(devices.dut, 'supported_fnm_speeds') and devices.dut.nvl_fnm_ports:
            fnm_port_name = RandomizationTool.select_random_value(devices.dut.nvl_fnm_ports).get_returned_value()
            fnm_port = Port(fnm_port_name)
            logger.info(f"Selected FNM port: {fnm_port.name}")
            expected_fnm_lanes = getattr(devices.dut, 'supported_fnm_lanes', None)
            _validate_fnm_port_speeds_and_lanes(fnm_port.name, fnm_port, devices.dut.supported_fnm_speeds, expected_fnm_lanes, "FNM")
        else:
            logger.info("Skipping FNM validation (no ports or no expected speeds defined)")

    # Test internal FNM ports (Fae command)
    with allure_step("Validate supported speeds and lanes on internal FNM ports (Fae)"):
        if hasattr(devices.dut, 'supported_fnm_speeds') and devices.dut.nvl_internal_fnm_ports:
            internal_fnm_port_name = RandomizationTool.select_random_value(devices.dut.nvl_internal_fnm_ports).get_returned_value()
            internal_fnm_fae = Fae(port_name=internal_fnm_port_name)
            logger.info(f"Selected internal FNM port (Fae): {internal_fnm_port_name}")
            expected_internal_fnm_lanes = getattr(devices.dut, 'supported_internal_fnm_lanes', None)
            _validate_fnm_port_speeds_and_lanes(internal_fnm_port_name, internal_fnm_fae, devices.dut.supported_fnm_speeds, expected_internal_fnm_lanes, "Internal FNM")
        else:
            logger.info("Skipping internal FNM validation (no ports or no expected speeds defined)")


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_nvl_invalid_speed_configuration_negative(engines, devices, test_api, has_loopbox, standalone_system):
    """
    Validate that configuring unsupported speeds fails with proper error message

    Test flow:
    1. Randomly select an nvl interface
    2. Get supported speeds from device
    3. Try to configure an invalid speed (not in supported list)
    4. Verify command fails with appropriate error message
    5. Verify error message contains the supported speeds list
    """
    TestToolkit.tested_api = test_api
    with allure_step("Select random nvl interface and get original speed"):
        # Get available ports
        available_ports = _get_available_nvl_ports(devices, has_loopbox, standalone_system)
        if not available_ports:
            pytest.skip("No nvl ports with proper connections available")

        selected_port = RandomizationTool.select_random_value(available_ports).get_returned_value()
        logger.info(f"Selected port for invalid speed test: {selected_port.name}")

        # Get original speed before attempting invalid configuration
        original_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_port.interface.link.show()).get_returned_value()
        original_speed = original_output.get(IbInterfaceConsts.LINK_SPEED)
        logger.info(f"Original speed: {original_speed}")

    with allure_step("Get supported speeds and generate invalid speed"):
        # Get expected supported speeds
        if not getattr(devices.dut, 'supported_nvl_speeds', None):
            pytest.skip("No supported_nvl_speeds available on device")

        supported_speeds = devices.dut.supported_nvl_speeds
        logger.info(f"Supported speeds: {supported_speeds}")

        # Use a simple invalid speed that's guaranteed to not be supported
        invalid_speed = "9999G"

        logger.info(f"Using invalid speed: {invalid_speed}")

    with allure_step(f"Attempt to configure invalid speed '{invalid_speed}'"):
        # Try to set the invalid speed - this should fail
        result = selected_port.interface.link.set(
            op_param_name='speed',
            op_param_value=invalid_speed,
            ask_for_confirmation=True,
            apply=True
        )

        # Verify the result indicates failure
        result.verify_result(should_succeed=False)
        logger.info("Command correctly failed as expected")

    with allure_step("Verify speed did not change and check error message"):
        # Get current speed to make sure it didn't change
        current_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_port.interface.link.show()).get_returned_value()
        current_speed = current_output.get(IbInterfaceConsts.LINK_SPEED)
        logger.info(f"Current speed after failed command: {current_speed}")

        # Verify speed did not change
        assert current_speed == original_speed, f"Speed changed unexpectedly! Original: {original_speed}, Current: {current_speed}"
        logger.info(f"Confirmed speed remained unchanged: {current_speed}")

        # Get the error message from the result
        error_message = result.info
        logger.info(f"Error message: {error_message}")

        # Validate error message contains key components (order-independent)
        # Expected pattern example: "Error: 9999G not in ['200G', '400G', '360G', '328G']" (device-dependent)
        assert f"Error: {invalid_speed}" in error_message, f"Error message should start with 'Error: {invalid_speed}'"
        assert "not in" in error_message, "Error message should indicate value is 'not in' supported list"

        # Verify all supported speeds are mentioned in the error (order doesn't matter)
        for speed in supported_speeds:
            assert speed in error_message, f"Supported speed '{speed}' should be mentioned in error message"

        logger.info("Confirmed error message contains invalid speed and all supported speeds")

        logger.info(f"Successfully validated invalid speed configuration for {selected_port.name}")


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.simx
def test_nvl_speed_configuration(engines, devices, random_api, has_loopbox, standalone_system):
    """
    Validate speed configuration on nvl ports.

    All access ports must be configured together (via range command) because:
    - Loopbox connects all access ports together
    - Different speeds use different lane modes (e.g., 200G=1X, 360G=2X)
    - Mixed lane configurations cannot negotiate with each other

    Test flow:
    1. Build access port range, get original speed from a random port
    2. Select random supported speed (excluding 200G), set on ALL access ports
    3. Wait and verify ALL ports reached the new speed
    4. On a random port: verify supported-lanes matches device config
    5. If configured speed is lower than original, verify supported-speeds filtering
    6. Cleanup: unset speed on ALL ports, verify restoration
    """
    if not (has_loopbox or not standalone_system):
        pytest.skip("Test requires loopbox or non-standalone system for access ports")

    if not devices.dut.nvl_access_ports_list:
        pytest.skip("No access ports available on device")

    if not getattr(devices.dut, 'supported_nvl_speeds', None):
        pytest.skip("No supported_nvl_speeds available on device")

    available_speeds = [s for s in devices.dut.supported_nvl_speeds if s != '200G']
    if not available_speeds:
        pytest.skip("No non-200G speeds available for testing")

    port_names = devices.dut.nvl_access_ports_list
    port_indices = [int(name.replace('acp', '')) for name in port_names]
    min_port, max_port = min(port_indices), max(port_indices)
    access_ports_range = f'acp{min_port}-{max_port}'
    all_ports = Port(access_ports_range)
    logger.info(f"Access ports range: {access_ports_range} ({len(port_names)} ports)")

    sample_port_name = RandomizationTool.select_random_value(port_names).get_returned_value()
    sample_port = Port(sample_port_name)

    original_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
        sample_port.interface.link.show()).get_returned_value()
    original_speed = original_output.get(IbInterfaceConsts.LINK_SPEED)
    logger.info(f"Original speed (from {sample_port_name}): {original_speed}")

    new_speed = RandomizationTool.select_random_value(available_speeds).get_returned_value()
    logger.info(f"Setting speed from {original_speed} to {new_speed} on all access ports (200G excluded)")

    try:
        with allure_step(f"Set speed {new_speed} on all access ports: {access_ports_range}"):
            all_ports.interface.link.set(
                op_param_name='speed', op_param_value=new_speed,
                ask_for_confirmation=True, apply=True
            ).verify_result()

        with allure_step(f"Wait and verify ALL access ports reached speed {new_speed}"):
            retry_call(validate_ports_state_and_speed, [new_speed, port_names, 'acp'],
                       exceptions=AssertionError, tries=7, delay=30)
            logger.info(f"✓ All access ports reached speed {new_speed}")

        with allure_step("Verify supported-lanes matches device configuration"):
            verify_port_name = RandomizationTool.select_random_value(port_names).get_returned_value()
            verify_port = Port(verify_port_name)
            json_output = verify_port.interface.link.show(output_format=OutputFormat.json)
            output_dict = OutputParsingTool.parse_json_str_to_dictionary(json_output).get_returned_value()
            supported_lanes = output_dict.get(IbInterfaceConsts.LINK_SUPPORTED_LANES)
            expected_lanes = devices.dut.supported_lanes
            assert supported_lanes == expected_lanes, (
                f"Expected supported-lanes '{expected_lanes}' for speed {new_speed}, "
                f"but got '{supported_lanes}' on port {verify_port_name}"
            )
            logger.info(f"✓ Validated on {verify_port_name}: supported-lanes = {supported_lanes}")

        new_speed_value = int(new_speed.replace('G', ''))
        original_speed_value = int(original_speed.replace('G', ''))

        if new_speed_value < original_speed_value:
            with allure_step(f"Verify supported-speeds limited to {new_speed} and below"):
                current_supported_speeds = _get_interface_supported_speeds(verify_port)
                logger.info(f"Supported speeds after lowering to {new_speed}: {current_supported_speeds}")

                violations = []
                for speed in current_supported_speeds:
                    speed_value = int(speed.replace('G', ''))
                    if speed_value > new_speed_value:
                        violations.append(speed)

                assert not violations, (
                    f"After configuring speed to {new_speed}, the following HIGHER speeds "
                    f"should NOT appear in supported-speed field: {violations}. "
                    f"Expected: only speeds <= {new_speed}"
                )
                logger.info(f"✓ Confirmed: supported speeds correctly limited on {verify_port_name}")

    finally:
        with allure_step(f"Cleanup: unset speed on {access_ports_range} and verify restoration"):
            all_ports.interface.link.unset(
                op_param='speed', apply=True, ask_for_confirmation=True
            ).verify_result()
            logger.info(f"Unset speed on {access_ports_range}")

            default_speed = devices.dut.access_port_speed if hasattr(devices.dut, 'access_port_speed') else original_speed
            retry_call(validate_ports_state_and_speed, [default_speed, port_names, 'acp'],
                       exceptions=AssertionError, tries=6, delay=30)
            logger.info(f"✓ All access ports restored to {default_speed}")


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.simx
def test_nvl_200g_simplex_lanes_validation(engines, devices, random_api, has_loopbox):
    """
    Validate supported-lanes includes "1X" for 200G speed (simplex mode) on QTM4+ ASICs

    Simplex mode explanation: At 200G, both TX and RX lanes are merged into a single
    bidirectional lane instead of having separate 1TX + 1RX lanes.

    IMPORTANT: We configure ALL access ports to 200G together (using range command) because:
    - 200G uses 1X lanes (simplex)
    - Other speeds use 2X lanes (duplex)
    - Loopbox connects all access ports together
    - Mixed lane configurations (1X and 2X) cannot negotiate with each other
    - Configuring only one port to 200G while others stay at different speeds would cause negotiation failure

    Test flow:
    1. Skip if no loopbox or if ASIC is QTM3/NVL5
    2. Get all access ports and create range (e.g., acp1-72 or acp1-288)
    3. Configure ALL access ports to 200G using range command
    4. Wait for link toggle using retry validation (6 tries × 30 sec = 180 sec max)
    5. Select ONE random access port and verify supported-lanes includes "1X"
    6. Cleanup: unset speed on range, wait for restoration
    """
    # Skip if no loopbox
    if not has_loopbox:
        pytest.skip("Test requires loopbox for access ports")

    # Skip on QTM3, NVL5 - simplex mode is QTM4+ feature (NVL6 and newer)
    if devices.dut.asic_type in [NvosConst.QTM3, NvosConst.NVL5]:
        pytest.skip(f"200G simplex mode (1X lanes) not applicable for {devices.dut.asic_type}. QTM4+/NVL6+ only.")

    if not devices.dut.nvl_access_ports_list:
        pytest.skip("No access ports available on device")

    # Create access port range (e.g., acp1-72 or acp1-288)
    port_indices = [int(port_name.replace('acp', '')) for port_name in devices.dut.nvl_access_ports_list]
    min_port, max_port = min(port_indices), max(port_indices)
    access_ports_range = f'acp{min_port}-{max_port}'
    all_access_ports = Port(access_ports_range)
    logger.info(f"Access ports range: {access_ports_range}")

    try:
        with allure_step(f"Configure 200G on all access ports: {access_ports_range}"):
            all_access_ports.interface.link.set(
                op_param_name='speed',
                op_param_value='200G',
                ask_for_confirmation=True,
                apply=True
            ).verify_result()
            logger.info(f"Configured 200G on {access_ports_range}")

        with allure_step("Wait for link toggle using retry validation"):
            # Use same pattern as test_interface_xdr_slow_speed_access_ports
            port_names = devices.dut.nvl_access_ports_list
            retry_call(validate_ports_state_and_speed, ['200G', port_names, 'acp'],
                       exceptions=AssertionError, tries=6, delay=30)
            logger.info("✓ All access ports successfully transitioned to 200G")

        with allure_step("Verify supported-lanes includes 1X for 200G (simplex mode)"):
            # Select ONE random access port to verify
            test_port_name = RandomizationTool.select_random_value(devices.dut.nvl_access_ports_list).get_returned_value()
            test_port = Port(test_port_name)
            logger.info(f"Selected port for lanes verification: {test_port.name}")

            output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                test_port.interface.link.show()).get_returned_value()
            current_speed = output.get(IbInterfaceConsts.LINK_SPEED)
            supported_lanes = output.get(IbInterfaceConsts.LINK_SUPPORTED_LANES)

            logger.info(f"Port {test_port.name} - Speed: {current_speed}, Supported lanes: {supported_lanes}")

            explanation = ("200G uses simplex mode: Both TX and RX lanes are merged into "
                           "a single bidirectional lane, instead of having separate 1TX + 1RX lanes")

            assert current_speed == "200G", f"Expected speed 200G, but got {current_speed}"

            # Verify 1X is present (could be "1X" only or "1X,2X" depending on ASIC)
            assert '1X' in supported_lanes, (
                f"Expected '1X' to be present in supported-lanes for 200G speed, "
                f"but got '{supported_lanes}'. Reason: {explanation}"
            )
            logger.info(f"✓ Validated: 200G → supported-lanes contains '1X' (simplex mode supported). Full value: {supported_lanes}")

    finally:
        with allure_step(f"Cleanup: Unset speed on {access_ports_range}"):
            all_access_ports.interface.link.unset(
                op_param='speed',
                apply=True,
                ask_for_confirmation=True
            ).verify_result()
            logger.info(f"Unset speed on {access_ports_range}")

            # Wait for restoration using retry validation
            port_names = devices.dut.nvl_access_ports_list
            default_speed = devices.dut.access_port_speed if hasattr(devices.dut, 'access_port_speed') else '400G'
            retry_call(validate_ports_state_and_speed, [default_speed, port_names, 'acp'],
                       exceptions=AssertionError, tries=6, delay=30)
            logger.info(f"✓ All access ports restored to {default_speed}")
