import logging
import random
import pytest
from retry.api import retry_call

from ngts.nvos_constants.constants_nvos import HealthConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.tests_nvos.interfaces.nvl_port.helpers import skip_if_no_trunk_links
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.IbInterfaceTool import IbInterfaceTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.tests_nvos.interfaces.test_ib_interface_state import verify_port_state
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli

from ngts.tests_nvos.constants import MINUTE

from .constants import (
    StressModeConsts,
    FatalModeConsts,
    PowerSavingConsts
)
from .helpers import (
    set_stress_mode_state,
    enable_and_validate_stress_mode,
    disable_and_validate_stress_mode,
    simulate_events,
    set_l1_req_en_for_all_ports,
    validate_all_ports_l1_cap,
    validate_health_status,
    validate_msg_in_syslog,
    pre_stress_checks,
    validate_stress_mode_effects,
    post_stress_checks
)

logger = logging.getLogger(__name__)


@pytest.mark.stress_mode
@pytest.mark.timeout(30 * MINUTE, func_only=True)
def test_stress_mode_configs(engines, devices, nv_command):
    """
    Validate stress mode correctly modifies system configuration and restores it.

    Test Objective:
        Verify that stress mode properly disables flex counters and L1 power saving,
        and that all settings are restored when stress mode is disabled.

    Test Flow:
        1. Pre-checks: Capture baseline configuration and verify system health
        2. Enable stress mode and validate activation
        3. Validate stress mode effects:
           - All flex counters disabled (PORT_STAT_COUNTER, PORT_AMBER_PDDR, etc.)
           - L1 power saving disabled on all ports
        4. Disable stress mode and validate deactivation
        5. Post-checks: Verify complete baseline restoration and system health

    Expected Results:
        - Flex counters: All tables disabled during stress mode
        - L1 Power Saving: Disabled on all ports during stress mode
        - Configuration Restoration: All settings return to baseline values
        - System Health: Remains 'OK' throughout the test
    """
    engine = engines.dut
    device = devices.dut

    # Pre-stress checks
    baseline = pre_stress_checks(engine, device)

    # Enable and validate stress mode
    enable_and_validate_stress_mode(engine)

    # Validate stress mode effects
    validate_stress_mode_effects(engine, device)

    # Disable and validate stress mode
    disable_and_validate_stress_mode(engine)

    # Post-stress checks
    post_stress_checks(engine, device, baseline)


@pytest.mark.stress_mode
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_stress_mode_port_admin_state_persistence(engines, devices):
    """
    Verify that stress mode prevents port state changes from affecting NVOS link state.

    Test Objective:
        Validate that during stress mode, hardware-level port admin state changes
        (via mlxreg PAOS register) do not trigger NVOS link state changes.

    Test Flow:
        1. Select a random port in 'up' state
        2. Enable stress mode and validate activation
        3. Set port admin state DOWN via mlxreg (PAOS register)
        4. Verify register shows admin_status is DOWN
        5. Verify NVOS still reports link state as UP (unchanged)
        6. Disable stress mode and validate deactivation
        7. Set port admin state UP via mlxreg
        8. Verify port returns to normal operation

    Expected Results:
        - Hardware Register: admin_status successfully changes to DOWN
        - NVOS Link State: Remains UP during stress mode (ignores hardware change)
        - Recovery: Port returns to UP state after stress mode disabled
        - System Stability: No interruption to link operation
    """
    engine = engines.dut
    skip_if_no_trunk_links(devices)

    with allure.step(f"Select {devices.dut.nvl_port_type} ports"):
        port_names = [port.name for port in RandomizationTool.select_random_ports(requested_ports_type=devices.dut.nvl_port_type, num_of_ports_to_select=0).get_returned_value() if port.name.startswith('sw')]
        up_ports = [Port(port_name) for port_name in port_names]
        selected_port = random.choice(up_ports)
        TestToolkit.update_tested_ports([selected_port])
        port_name = selected_port.name

    # Enable stress mode
    enable_and_validate_stress_mode(engine)

    try:
        # Set admin state down and verify disconnect between mlxreg and NVOS
        with allure.step("Set port admin DOWN and verify mlxreg vs NVOS"):

            with allure.step(f"Set port {port_name} admin state DOWN via mlxreg PAOS"):
                IbInterfaceTool.set_port_admin_state_paos_down(engine, port_name, sleep=5)

            with allure.step("Verify mlxreg shows admin_status is DOWN"):
                paos_output = IbInterfaceTool.get_port_admin_state_paos(engine, port_name, grep_pattern="admin_status")
                logger.info(f"PAOS register output: {paos_output}")
                # The register should show admin_status changed
                ValidationTool.compare_values(paos_output, '0x00000001').verify_result()

            with allure.step("Verify NVOS shows link state is UP"):
                output_dict = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                    selected_port.interface.link.show()).get_returned_value()

                verify_port_state(output_dict, NvosConsts.LINK_STATE_UP)

        # Disable stress mode
        disable_and_validate_stress_mode(engine)

        # Set admin state back up and verify
        with allure.step("Post-checks: Set port admin UP and verify recovery"):
            with allure.step(f"Set port {port_name} admin state UP via mlxreg PAOS"):
                IbInterfaceTool.set_port_admin_state_paos_up(engine, port_name, sleep=5)

            with allure.step("Verify NVOS shows link state is UP"):
                output_dict = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                    selected_port.interface.link.show()).get_returned_value()

                verify_port_state(output_dict, NvosConsts.LINK_STATE_UP)

    finally:
        # Cleanup port state (stress mode cleanup handled by fixture)
        with allure.step("Cleanup: Restore port admin state"):
            IbInterfaceTool.set_port_admin_state_paos_up(engine, port_name, sleep=5)


@pytest.mark.stress_mode
@pytest.mark.timeout(30 * MINUTE, func_only=True)
def test_stress_mode_l1_power_saving_persistence(engines, devices):
    """
    Verify L1 power saving can be controlled during stress mode via hardware registers.

    Test Objective:
        Validate that L1 power saving configuration changes via PPSLC register
        are properly reflected in PPSLS register during stress mode.

    Test Flow:
        1. Enable stress mode and validate activation
        2. Enable L1 power saving:
           - Set l1_req_en=1 on PPSLC register for all ports
           - Verify all ports show l1_cap=1 via PPSLS register
        3. Disable L1 power saving:
           - Set l1_req_en=0 on PPSLC register for all ports
           - Verify all ports show l1_cap=0 via PPSLS register
        4. Disable stress mode and validate deactivation

    Expected Results:
        - PPSLC Register: l1_req_en successfully set on all ports
        - PPSLS Register: l1_cap accurately reflects configuration changes
        - System Stability: No crashes or hangs during register operations
        - All Ports: Consistent behavior across all port instances
    """
    engine = engines.dut
    device = devices.dut

    # Enable stress mode
    enable_and_validate_stress_mode(engine)

    # Test L1 power saving enable/disable cycle
    try:
        with allure.step("Enable L1 request on all ports and verify"):
            with allure.step("Set l1_req_en=1 on PPSLC register for all ports"):
                set_l1_req_en_for_all_ports(engine, device, PowerSavingConsts.L1_REQ_EN_ENABLED).verify_result()

            with allure.step("Verify all ports show l1_cap=1 via PPSLS register"):
                validate_all_ports_l1_cap(engine, device, PowerSavingConsts.L1_CAP_ENABLED).verify_result()

    finally:
        # Cleanup L1 state (stress mode cleanup handled by fixture)
        with allure.step("Cleanup: Disable L1 request on all ports"):
            with allure.step("Set l1_req_en=0 on PPSLC register for all ports"):
                set_l1_req_en_for_all_ports(engine, device, PowerSavingConsts.L1_REQ_EN_DISABLED).verify_result()

            with allure.step("Verify all ports show l1_cap=0 via PPSLS register"):
                validate_all_ports_l1_cap(engine, device, PowerSavingConsts.L1_CAP_DISABLED).verify_result()


@pytest.mark.dut_reboot
@pytest.mark.stress_mode
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_stress_mode_fatal_no_reboot(engines, devices, nv_command):
    """
    Verify that stress mode suppresses automatic reboot on fatal events.

    Test Objective:
        Validate that when stress mode is enabled, fatal health events do NOT
        trigger automatic system reboot. The system should enter fatal state
        but remain operational for debugging.

    Test Flow:
        1. Pre-checks: Capture baseline configuration
        2. Enable stress mode and validate activation
        3. Trigger fatal events (FW assert + FW fatal cause)
        4. Verify system enters FATAL health status
        5. Verify syslog message: "suppressing fatal recovery actions"
        6. Trigger additional fatal events
        7. Verify system still in FATAL state without reboot
        8. Generate tech-support for debugging
        9. Cleanup: Reboot system and verify recovery to OK

    Expected Results:
        - Health Status: Transitions from OK to FATAL
        - Reboot Suppression: System does NOT automatically reboot
        - Syslog: Contains "suppressing fatal recovery actions" message
        - Multiple Events: Additional fatal events also suppressed
        - Recovery: System returns to OK after manual reboot
    """
    engine = engines.dut
    device = devices.dut
    system = nv_command.system

    with allure.step("Configure fatal mode settings"):
        nv_command.fae.system.fatal.set(op_param_name="clear-time", op_param_value=FatalModeConsts.CLEAR_TIME, apply=True)
        NvueGeneralCli.save_config(engines.dut)

    with allure.step("Pre-checks: Select random ASIC"):
        random_asic = RandomizationTool.select_random_asics().get_returned_value()[0]
        logger.info(f"Selected ASIC: {random_asic}")

    with allure.step("Capture baseline"):
        baseline = pre_stress_checks(engine, device)

    try:
        # Enable and validate stress mode
        enable_and_validate_stress_mode(engine)

        with allure.step(f"Trigger fatal mode with no restart"):
            simulate_events(engine, random_asic)
            retry_call(validate_health_status, [HealthConsts.FATAL], exceptions=AssertionError, tries=6, delay=10)

        with allure.step(f"Verify syslog message: {FatalModeConsts.FATAL_RECOVERY_ACTIONS_SUPPRESSED_MSG}"):
            validate_msg_in_syslog(engine, FatalModeConsts.FATAL_RECOVERY_ACTIONS_SUPPRESSED_MSG)

        with allure.step(f"Simulate more events and assert still no restart"):
            simulate_events(engine, random_asic)
            retry_call(validate_health_status, [HealthConsts.FATAL], exceptions=AssertionError, tries=6, delay=10)

        with allure.step("Generate tech-support"):
            tech_support_folder, duration = system.techsupport.action_generate()

    finally:
        with allure.step("Cleanup reboot"):
            system.reboot.action_reboot().verify_result()

        with allure.step("Cleanup: Wait for system to exit fatal mode after reboot"):
            # After manual reboot, system should exit fatal mode once boot completes
            # Retry to allow time for full system initialization
            retry_call(validate_health_status, [HealthConsts.OK], exceptions=AssertionError, tries=6, delay=10)

        with allure.step("Cleanup: System recovered from fatal events"):
            post_stress_checks(engine, device, baseline)
            system.techsupport.files.file_name[system.techsupport.file_name].action_delete()

        with allure.step("Cleanup: Revert fatal-mode settings"):
            nv_command.fae.system.fatal.unset(apply=True)
            NvueGeneralCli.save_config(engine)


@pytest.mark.skip(reason="TODO: Actual stress mode test not implemented - waiting for stress mode integration")
@pytest.mark.stress_mode
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_stress_mode_with_actual_traffic(engines, devices, nv_command):
    """
    TODO: Test stress mode with actual traffic stress testing.
    """
    pass
