import logging
import random
import re
import time
import pytest
from retry.api import retry_call

from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.system.Profile import CORE_CLOCK_KEY
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.cluster.cluster_tools import summarize_switch_ports
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.interfaces.nvl_port.helpers import validate_ports_state_and_speed, wait_for_ports_state_and_speed
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.tools.test_utils.allure_utils import step as allure_step
from ngts.tests_nvos.system.factory_reset.helpers import get_current_time
from ngts.tests_nvos.system.test_system_factory_reset import execute_reset_factory

logger = logging.getLogger(__name__)

CORE_CLOCK_REGISTERS_DUMP_FILE = 'core_clock_registers'
CORE_CLK_SPEED_VALID_LINE_RE = re.compile(r'^core_clk_speed_valid\s*\|\s*(0x[0-9a-fA-F]+|\d+)\s*$', re.MULTILINE)
CORE_CLK_SPEED_LINE_RE = re.compile(r'^core_clk_speed\s*\|\s*(0x[0-9a-fA-F]+)\s*$', re.MULTILINE)


@pytest.mark.dut_reboot
@pytest.mark.fae
@pytest.mark.interface
@pytest.mark.timeout(30 * MINUTE, func_only=True)
def test_fae_core_clock_main_flow(engines, devices, has_loopbox, standalone_system, test_name, random_api):
    """ Validate core-clock is updated as expected.

    1. Show operational core-clock.
    2. Choose another supported core-clock.
    3. Change interface speed to a supported speed for the new core-clock (when required).
    4. Change FAE core-clock to the new core-clock.
    5. Generate tech-support and verify core-clock registers in the dump.
    6. Clean up: tech-support bundle.
    7. Clean up: restore original FAE core-clock.
    8. Cleanup: unset link speed on all access ports and verify default speeds.
    """
    _skip_test_if_needed(devices, has_loopbox, standalone_system)

    system = System()
    profile = Fae().system.profile
    supported_core_clocks = devices.dut.fae_supported_core_clocks
    default_speed = devices.dut.access_port_speed

    with allure_step("Show operational core-clock"):
        core_clock = (OutputParsingTool.parse_json_str_to_dictionary(Fae().system.profile.show(dut_engine=engines.dut))
                      .get_returned_value().get(CORE_CLOCK_KEY))
        assert core_clock is not None and core_clock in supported_core_clocks, (
            "fae system profile output missing core-clock or unsupported core-clock"
        )
        assert core_clock == devices.dut.default_core_clock, (
            f"expected operational core-clock {devices.dut.default_core_clock} MHz at test start, got {core_clock}"
        )

    other_core_clock, illegal_speed = _select_other_core_clock_and_illegal_speed(supported_core_clocks, core_clock)

    port_names = devices.dut.nvl_access_ports_list
    access_ports_range = summarize_switch_ports(port_names)
    all_ports = Port(access_ports_range)
    logger.info(f"Access ports range: {access_ports_range} ({len(port_names)} ports)")

    sample_port_name = RandomizationTool.select_random_value(port_names).get_returned_value()
    sample_port = Port(sample_port_name)
    original_speed = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
        sample_port.interface.link.show()).get_returned_value().get(IbInterfaceConsts.LINK_SPEED)
    logger.info(f"Original speed (from {sample_port_name}): {original_speed}")

    speed_changed = False
    try:
        if illegal_speed and original_speed == illegal_speed:
            # Choose a random link speed valid on the target core-clock
            new_speed = _random_nvl_speed_for_core_clock(supported_core_clocks, other_core_clock)

            with allure_step(f"Set speed {new_speed} on all access ports: {access_ports_range}"):
                speed_changed = True
                all_ports.interface.link.set(
                    op_param_name='speed', op_param_value=new_speed, ask_for_confirmation=True, apply=True).verify_result()
                TestToolkit.GeneralApi[random_api].save_config(engines.dut)

            with allure_step(f"Wait and verify ALL access ports reached speed {new_speed}"):
                wait_for_ports_state_and_speed(devices, new_speed, port_names, 'acp')
                logger.info(f"✓ All access ports reached speed {new_speed}")

        with allure_step(f"Change FAE core-clock to {other_core_clock} MHz"):
            profile.action_change_core_clock(other_core_clock, random_api, engine=engines.dut, send_user_confirmation="y").verify_result()

        with allure_step(f"Verify FAE system profile core-clock is {other_core_clock}"):
            assert (OutputParsingTool.parse_json_str_to_dictionary(profile.show(dut_engine=engines.dut))
                    .get_returned_value().get(CORE_CLOCK_KEY)) == other_core_clock

        with allure_step(f"Generate tech-support and verify core-clock registers ({other_core_clock} MHz)"):
            _generate_techsupport_and_verify_core_clock_registers(engines, other_core_clock, system)

    finally:
        if system.techsupport.file_name:
            with allure_step("Clean up: tech-support bundle"):
                system.techsupport.cleanup(engines.dut)
                system.techsupport.files.file_name[system.techsupport.file_name].action_delete()

        with allure_step(f"Restore original FAE core-clock {core_clock} MHz"):
            profile.action_change_core_clock(core_clock, random_api, engine=engines.dut, flags="force").verify_result()

        with allure_step(f"Verify FAE system profile core-clock is {core_clock}"):
            assert (OutputParsingTool.parse_json_str_to_dictionary(profile.show(dut_engine=engines.dut))
                    .get_returned_value().get(CORE_CLOCK_KEY)) == core_clock

        if speed_changed:
            with allure_step(f"Cleanup: unset speed on {access_ports_range} and verify restoration"):
                all_ports.interface.link.unset(
                    op_param='speed', apply=True, ask_for_confirmation=True).verify_result()

                wait_for_ports_state_and_speed(devices, default_speed, port_names, 'acp')
                logger.info(f"✓ All access ports restored to {default_speed}")


@pytest.mark.dut_reboot
@pytest.mark.fae
@pytest.mark.interface
@pytest.mark.timeout(30 * MINUTE, func_only=True)
def test_fae_core_clock_invalid_flow(engines, devices, has_loopbox, standalone_system, test_name, random_api):
    """ Validate invalid core-clock values and blocked changes when port speed is illegal for the target clock.

    1. Show operational core-clock.
    2. Choose another supported core-clock with illegal link speed (skip if none).
    3. Attempt change FAE core-clock to invalid MHz (expect failure).
    4. Set all access ports to the illegal speed for the target clock.
    5. Attempt change to the other core-clock (expect failure).
    6. Set all access ports to a speed supported on the target core-clock.
    7. Change FAE core-clock to the other core-clock (expect success).
    8. On one access port, attempt the illegal speed (expect failure).
    9. Restore original FAE core-clock.
    10. Unset link speed on all access ports and verify default speeds.
    """
    _skip_test_if_needed(devices, has_loopbox, standalone_system)

    supported_core_clocks = devices.dut.fae_supported_core_clocks
    with allure_step("Show operational core-clock"):
        core_clock = (OutputParsingTool.parse_json_str_to_dictionary(Fae().system.profile.show(dut_engine=engines.dut))
                      .get_returned_value().get(CORE_CLOCK_KEY))
        assert core_clock is not None and core_clock in supported_core_clocks, (
            "fae system profile output missing core-clock or unsupported core-clock"
        )

    other_core_clock, illegal_speed = _select_other_core_clock_and_illegal_speed(supported_core_clocks, core_clock)
    if illegal_speed is None:
        pytest.skip("other_core_clock supports all link speeds that the operational core-clock allows")

    port_names = devices.dut.nvl_access_ports_list
    access_ports_range = summarize_switch_ports(port_names)
    all_ports = Port(access_ports_range)
    logger.info(f"Access ports range: {access_ports_range} ({len(port_names)} ports)")

    sample_port_name = RandomizationTool.select_random_value(port_names).get_returned_value()
    sample_port = Port(sample_port_name)
    profile = Fae().system.profile
    try:
        illegal_core_clock = max(supported_core_clocks) + random.randint(1, 500)
        with allure_step(f"Attempt FAE change to invalid core-clock {illegal_core_clock} MHz"):
            profile.action_change_core_clock(illegal_core_clock, random_api, engine=engines.dut, send_user_confirmation="y").verify_result(
                should_succeed=False, expected_value="not one of")

        with allure_step(f"Set speed {illegal_speed} on all access ports: {access_ports_range}"):
            all_ports.interface.link.set(
                op_param_name='speed', op_param_value=illegal_speed, ask_for_confirmation=True, apply=True).verify_result()
            TestToolkit.GeneralApi[random_api].save_config(engines.dut)

        with allure_step(f"Wait and verify ALL access ports reached speed {illegal_speed}"):
            wait_for_ports_state_and_speed(devices, illegal_speed, port_names, 'acp')

        with allure_step(f"Attempt FAE core-clock change to {other_core_clock} MHz while {sample_port_name} is at illegal speed {illegal_speed}."):
            profile.action_change_core_clock(other_core_clock, random_api, engine=engines.dut, send_user_confirmation="y").verify_result(
                should_succeed=False, expected_value="not supported")

        new_speed = _random_nvl_speed_for_core_clock(supported_core_clocks, other_core_clock)
        with allure_step(f"Set speed {new_speed} on all access ports: {access_ports_range}"):
            all_ports.interface.link.set(
                op_param_name='speed', op_param_value=new_speed, ask_for_confirmation=True, apply=True).verify_result()
            TestToolkit.GeneralApi[random_api].save_config(engines.dut)

        with allure_step(f"Wait and verify ALL access ports reached speed {new_speed}"):
            wait_for_ports_state_and_speed(devices, new_speed, port_names, 'acp')
            logger.info(f"✓ All access ports reached speed {new_speed}")

        with allure_step(f"Change FAE core-clock to {other_core_clock} MHz with compatible access ports"):
            profile.action_change_core_clock(other_core_clock, random_api, engine=engines.dut, send_user_confirmation="y").verify_result()

        with allure_step(f"Verify FAE system profile core-clock is {other_core_clock}"):
            assert (OutputParsingTool.parse_json_str_to_dictionary(profile.show(dut_engine=engines.dut))
                    .get_returned_value().get(CORE_CLOCK_KEY)) == other_core_clock

        with allure_step(f"Set speed illegal speed {illegal_speed} on access port: {sample_port_name}"):
            sample_port.interface.link.set(
                op_param_name='speed', op_param_value=illegal_speed, ask_for_confirmation=True, apply=True).verify_result(should_succeed=False, expected_value="not supported")

    finally:
        with allure_step(f"Restore original FAE core-clock {core_clock} MHz"):
            profile.action_change_core_clock(core_clock, random_api, engine=engines.dut, flags="force").verify_result()

        with allure_step(f"Verify FAE system profile core-clock is {core_clock}"):
            assert (OutputParsingTool.parse_json_str_to_dictionary(profile.show(dut_engine=engines.dut))
                    .get_returned_value().get(CORE_CLOCK_KEY)) == core_clock

        with allure_step(f"Cleanup: unset speed on {access_ports_range} and verify restoration"):
            all_ports.interface.link.unset(
                op_param='speed', apply=True, ask_for_confirmation=True).verify_result()
            logger.info(f"Unset speed on {access_ports_range}")

            default_speed = devices.dut.access_port_speed
            wait_for_ports_state_and_speed(devices, default_speed, port_names, 'acp')
            logger.info(f"✓ All access ports restored to {default_speed}")


@pytest.mark.dut_reboot
@pytest.mark.fae
@pytest.mark.interface
@pytest.mark.timeout(30 * MINUTE, func_only=True)
def test_fae_core_clock_ports_down(engines, devices, has_loopbox, standalone_system, test_name, random_api):
    """ Validate access ports go down after core-clock change when no speed is saved and reset factory behaves as expected.

    1. Show operational core-clock.
    2. Require default access-port speed illegal on alternate core-clock (skip otherwise).
    3. Unset link speed on all access ports.
    4. Change FAE core-clock to the other core-clock.
    5. Verify access ports are down; show link diagnostics.
    6. Run factory reset.
    7. Verify core-clock is platform default after reset.
    8. Generate tech-support and verify core-clock registers in the dump.
    9. Clean up: tech-support bundle.
    10. If needed: restore original FAE core-clock, unset link speeds, verify default speeds.
    """
    _skip_test_if_needed(devices, has_loopbox, standalone_system)

    supported_core_clocks = devices.dut.fae_supported_core_clocks
    with allure_step("Show operational core-clock"):
        core_clock = (OutputParsingTool.parse_json_str_to_dictionary(Fae().system.profile.show(dut_engine=engines.dut))
                      .get_returned_value().get(CORE_CLOCK_KEY))
        assert core_clock is not None and core_clock in supported_core_clocks, (
            "fae system profile output missing core-clock or unsupported core-clock"
        )

    default_speed = devices.dut.access_port_speed
    other_core_clock, illegal_speed = _select_other_core_clock_and_illegal_speed(supported_core_clocks, core_clock)
    if illegal_speed is None or default_speed != illegal_speed:
        pytest.skip("other_core_clock supports all link speeds that the operational core-clock allows")

    port_names = devices.dut.nvl_access_ports_list
    access_ports_range = summarize_switch_ports(port_names)
    all_ports = Port(access_ports_range)
    logger.info(f"Access ports range: {access_ports_range} ({len(port_names)} ports)")

    with allure_step(f"unset speed on {access_ports_range} to ensure clean start"):
        all_ports.interface.link.unset(
            op_param='speed', apply=True, ask_for_confirmation=True).verify_result()
        logger.info(f"Unset speed on {access_ports_range}")

        retry_call(validate_ports_state_and_speed, [default_speed, port_names, 'acp'],
                   exceptions=AssertionError, tries=6, delay=30)

    system = System()
    profile = Fae().system.profile
    need_to_do_cleanup = False

    sample_port_name = RandomizationTool.select_random_value(port_names).get_returned_value()
    sample_port = Port(sample_port_name)
    try:
        with allure_step(f"Change FAE core-clock to {other_core_clock} MHz while default speed is invalid on target clock"):
            profile.action_change_core_clock(other_core_clock, random_api, engine=engines.dut, send_user_confirmation="y").verify_result()

        with allure_step(f"Verify FAE system profile core-clock is {other_core_clock}"):
            assert (OutputParsingTool.parse_json_str_to_dictionary(profile.show(dut_engine=engines.dut))
                    .get_returned_value().get(CORE_CLOCK_KEY)) == other_core_clock

        need_to_do_cleanup = True

        with allure_step("Verify access ports are down after core-clock change"):
            retry_call(validate_ports_state_and_speed, [default_speed, port_names, 'acp'],
                       {"state": NvosConsts.LINK_STATE_DOWN}, exceptions=AssertionError, tries=10, delay=30)

        with allure_step("show link down reason"):
            logger.info(sample_port.interface.link.diagnostics.show())

        with allure_step("Run Reset factory"):
            current_time = get_current_time(engines)
            execute_reset_factory(engines=engines, system=system, operation=devices.dut.reset_factory,
                                  flag="", current_time=current_time, test_name=test_name)

        need_to_do_cleanup = False

        with allure_step(f"Verify core-clock is default ({devices.dut.default_core_clock}) after reset"):
            post_reset_core_clock = (OutputParsingTool.parse_json_str_to_dictionary(Fae().system.profile.show(dut_engine=engines.dut))
                                     .get_returned_value().get(CORE_CLOCK_KEY))
            assert post_reset_core_clock == devices.dut.default_core_clock, (
                f'expected core-clock {devices.dut.default_core_clock} after factory reset, got {post_reset_core_clock}'
            )

        with allure_step(f"Generate tech-support and verify core-clock registers ({devices.dut.default_core_clock} MHz)"):
            _generate_techsupport_and_verify_core_clock_registers(engines, devices.dut.default_core_clock, system, 0)

    finally:
        if system.techsupport.file_name:
            with allure_step("Clean up: tech-support bundle"):
                system.techsupport.cleanup(engines.dut)
                system.techsupport.files.file_name[system.techsupport.file_name].action_delete()

        if need_to_do_cleanup:
            with allure_step(f"Restore original FAE core-clock {core_clock} MHz"):
                profile.action_change_core_clock(core_clock, random_api, engine=engines.dut, flags="force").verify_result()

            with allure_step(f"Verify FAE system profile core-clock is {core_clock}"):
                assert (OutputParsingTool.parse_json_str_to_dictionary(profile.show(dut_engine=engines.dut))
                        .get_returned_value().get(CORE_CLOCK_KEY)) == core_clock

            with allure_step(f"Cleanup: unset speed on {access_ports_range} and verify restoration"):
                all_ports.interface.link.unset(
                    op_param='speed', apply=True, ask_for_confirmation=True).verify_result()
                logger.info(f"Unset speed on {access_ports_range}")

                retry_call(validate_ports_state_and_speed, [default_speed, port_names, 'acp'],
                           exceptions=AssertionError, tries=6, delay=30)
                logger.info(f"✓ All access ports restored to {default_speed}")


def _random_nvl_speed_for_core_clock(supported_core_clocks, core_clock_mhz):
    speeds = list(supported_core_clocks.get(core_clock_mhz) or [])
    assert speeds, (
        f"fae_supported_core_clocks must list NVL speeds for core-clock {core_clock_mhz} MHz"
    )
    return RandomizationTool.select_random_value(speeds).get_returned_value()


def _select_other_core_clock_and_illegal_speed(supported_core_clocks, core_clock):
    assert isinstance(supported_core_clocks, dict), (
        "fae_supported_core_clocks must be a dict mapping core-clock MHz to allowed NVL speeds"
    )
    other_core_clocks = [c for c in supported_core_clocks if c != core_clock]
    assert other_core_clocks, "expected at least two supported FAE core clocks"
    other_core_clock = RandomizationTool.select_random_value(other_core_clocks).get_returned_value()

    # pick a NVL link speed that is allowed at core_clock but not allowed at other_core_clock.
    illegal_speed = None
    core_speeds = set(supported_core_clocks.get(core_clock) or [])
    other_speeds = set(supported_core_clocks.get(other_core_clock) or [])
    illegal_candidates = sorted(core_speeds - other_speeds)
    if illegal_candidates:
        illegal_speed = RandomizationTool.select_random_value(illegal_candidates).get_returned_value()

    return other_core_clock, illegal_speed


def _generate_techsupport_and_verify_core_clock_registers(engines, other_core_clock, system, expected_valid=1):
    with allure_step("Generate tech support"):
        system.techsupport.action_generate()
        system.techsupport.extract_techsupport_files(engines.dut)

    with allure_step("Check core_clock_registers exists in tech-support dump"):
        techsupport_dump_files = system.techsupport.get_techsupport_files_list(
            engines.dut, "dump"
        )
        assert CORE_CLOCK_REGISTERS_DUMP_FILE in techsupport_dump_files, (
            f"Expected {CORE_CLOCK_REGISTERS_DUMP_FILE!r} in tech-support dump/. "
            f"Got: {techsupport_dump_files}"
        )

    with allure_step(
        f"Verify core_clock_registers: core_clk_speed_valid={expected_valid}, "
        f"core_clk_speed={other_core_clock} MHz (0x{other_core_clock:08x})"
    ):
        _assert_core_clock_registers_dump_matches_engine(engines.dut, system.techsupport, other_core_clock, expected_valid=expected_valid)


def _assert_core_clock_registers_dump_matches_engine(engine, techsupport, expected_mhz, expected_valid=1):
    folder = techsupport.file_name.replace('.tar.gz', '')
    path = f'{SystemConsts.TECHSUPPORT_FILES_PATH}{folder}/dump/{CORE_CLOCK_REGISTERS_DUMP_FILE}'
    content = engine.run_cmd(f'sudo cat {path}')
    valid_vals = []
    for m in CORE_CLK_SPEED_VALID_LINE_RE.finditer(content):
        token = m.group(1)
        valid_vals.append(int(token, 0) if token.lower().startswith('0x') else int(token))
    speed_vals = [int(m.group(1), 16) for m in CORE_CLK_SPEED_LINE_RE.finditer(content)]
    assert valid_vals, f'no core_clk_speed_valid entries parsed from {path}'
    assert speed_vals, f'no core_clk_speed entries parsed from {path}'
    expected_hex = f'0x{expected_mhz:08x}'
    for i, v in enumerate(valid_vals):
        assert v == expected_valid, (
            f'expected core_clk_speed_valid {expected_valid} everywhere, got {v!r} at occurrence {i + 1}'
        )
    for i, s in enumerate(speed_vals):
        assert s == expected_mhz, (
            f'expected core_clk_speed {expected_mhz} ({expected_hex}), got {s} (0x{s:08x}) at occurrence {i + 1}'
        )


def _skip_test_if_needed(devices, has_loopbox, standalone_system):
    with allure_step("Check if test should be skipped"):
        if not getattr(devices.dut, "fae_supported_core_clocks", None):
            pytest.skip("FAE core-clock tests not defined for this switch (fae_supported_core_clocks unset)")
        if not (has_loopbox or not standalone_system):
            pytest.skip("Test requires loopbox or non-standalone system for access ports")
        if not devices.dut.nvl_access_ports_list:
            pytest.skip("No access ports available on device")
