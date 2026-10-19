import pytest

from ngts.nvos_tools.acl.acl import logger
import ngts.nvos_tools.infra.Tools as Tools
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import DelayedRecovery
from ngts.nvos_tools.infra.Fae import Fae
from ngts.tests_nvos.interfaces.delayed_recovery.constants import (
    CUSTOM_LOSS_TH_A,
    CUSTOM_LOSS_TH_B,
    CUSTOM_RETRY_TH_A,
    CUSTOM_RETRY_TH_B,
    FORCE_DISABLED,
    FORCE_ENABLED,
    OPER_ENABLED_RETRY_TH,
    STATE_DISABLED,
    STATE_ENABLED,
    STATE_FW_DEFAULT,
    admin_values,
    force_values,
    oper_values,
)
from ngts.tests_nvos.interfaces.delayed_recovery.helpers import (
    apply_config,
    delayed_recovery_expected,
    get_enabled_loss_th,
    set_delayed_recovery_values,
    unset_delayed_recovery,
    validate_expected_values,
    validate_expected_values_by_rev,
    wait_for_delayed_recovery_ports,
)
from ngts.tools.test_utils import allure_utils as allure


@pytest.mark.phy
def test_delayed_recovery_default_values(engines, devices, topology_obj):
    """
    Test flow:
        1. pick random port with up state
        2. run nv show fae interface <selected port> link delayed-recovery
        3. verify default values
        4. pick random port with down state
        5. run nv show fae interface <selected port> link delayed-recover
        6. verify default values
    """
    with allure.step("Test delayed recovery default values"):
        with allure.independent_step("Test for up state port"):
            selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state="up").get_returned_value()

            with allure.independent_step(f"Verify default values for {selected_port.name}"):
                expected_output = {
                    DelayedRecovery.DELAYED_RECOVERY_LOSS_TH: devices.dut.default_delayed_recovery_values["OPER_DISABLED_LOSS_TH"],
                    DelayedRecovery.DELAYED_RECOVERY_RETRY_TH: devices.dut.default_delayed_recovery_values["OPER_DISABLED_RETRY_TH"],
                    DelayedRecovery.DELAYED_RECOVERY_STATE: STATE_DISABLED,
                    DelayedRecovery.DELAYED_RECOVERY_LOSS_TH_FORCE: DelayedRecovery.DELAYED_RECOVERY_DEFAULT_FORCE_LOSS_TH,
                    DelayedRecovery.DELAYED_RECOVERY_RETRY_TH_FORCE: DelayedRecovery.DELAYED_RECOVERY_DEFAULT_FORCE_RETRY_TH,
                    DelayedRecovery.DELAYED_RECOVERY_STATE_FORCE: DelayedRecovery.DELAYED_RECOVERY_DEFAULT_FORCE_STATE
                }
                validate_expected_values(Fae(port_name=selected_port.name), expected_output)

        with allure.independent_step("Test for down state port"):
            selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state="down").ignore_result()

            if not selected_port.result:
                logger.info("No down port was found")
                return

            selected_port = selected_port.get_returned_value()
            with allure.independent_step(f"Verify default values for {selected_port.name}"):
                expected_output = {
                    DelayedRecovery.DELAYED_RECOVERY_LOSS_TH_FORCE: DelayedRecovery.DELAYED_RECOVERY_DEFAULT_FORCE_LOSS_TH,
                    DelayedRecovery.DELAYED_RECOVERY_RETRY_TH_FORCE: DelayedRecovery.DELAYED_RECOVERY_DEFAULT_FORCE_RETRY_TH,
                    DelayedRecovery.DELAYED_RECOVERY_STATE_FORCE: DelayedRecovery.DELAYED_RECOVERY_DEFAULT_FORCE_STATE
                }
                validate_expected_values(Fae(port_name=selected_port.name), expected_output)


@pytest.mark.phy
def test_delayed_recovery_set_unset_values(engines, devices, topology_obj, register_cleanup,
                                           delayed_recovery_ports, disable_logic_relock):
    """
    Test flow:
        1. pick connected switch-switch ports
        2. verify no-force delayed recovery scenarios
    """
    with allure.step("get connected ports (peer ports)"):
        selected_port, selected_peer_port = delayed_recovery_ports
        register_cleanup(lambda: unset_delayed_recovery(delayed_recovery_ports))

    # FW resolves a different enabled-mode threshold per module type, so the expected
    # value has to be read from the port rather than hardcoded.
    enabled_loss_th = get_enabled_loss_th(selected_port)

    no_force_scenarios = [
        (
            "Default state after init",
            {},
            {
                selected_port: delayed_recovery_expected(
                    admin_values(),
                    oper_values(
                        loss_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_LOSS_TH"],
                        retry_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_RETRY_TH"]
                    )
                ),
                selected_peer_port: delayed_recovery_expected(
                    admin_values(),
                    oper_values(
                        loss_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_LOSS_TH"],
                        retry_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_RETRY_TH"]
                    )
                ),
            },
        ),
        (
            "Enable state on one side",
            {
                selected_port: {
                    DelayedRecovery.DELAYED_RECOVERY_STATE: STATE_ENABLED,
                },
            },
            {
                selected_port: delayed_recovery_expected(
                    admin_values(state=STATE_ENABLED),
                    oper_values(
                        state=STATE_ENABLED,
                        loss_th=enabled_loss_th,
                        retry_th=OPER_ENABLED_RETRY_TH,
                    ),
                ),
                selected_peer_port: delayed_recovery_expected(
                    admin_values(),
                    oper_values(
                        loss_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_LOSS_TH"],
                        retry_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_RETRY_TH"]
                    )
                ),
            },
        ),
        (
            "Disable state on one side",
            {
                selected_port: {
                    DelayedRecovery.DELAYED_RECOVERY_STATE: STATE_DISABLED,
                },
            },
            {
                selected_port: delayed_recovery_expected(
                    admin_values(state=STATE_DISABLED),
                    oper_values(
                        loss_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_LOSS_TH"],
                        retry_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_RETRY_TH"]
                    )
                ),
                selected_peer_port: delayed_recovery_expected(
                    admin_values(state=STATE_FW_DEFAULT),
                    oper_values(
                        loss_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_LOSS_TH"],
                        retry_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_RETRY_TH"]
                    )
                ),
            },
        ),
        (
            "Set custom thresholds on both sides",
            {
                selected_port: {
                    DelayedRecovery.DELAYED_RECOVERY_STATE: STATE_ENABLED,
                    DelayedRecovery.DELAYED_RECOVERY_LOSS_TH: CUSTOM_LOSS_TH_A,
                    DelayedRecovery.DELAYED_RECOVERY_RETRY_TH: CUSTOM_RETRY_TH_A,
                },
                selected_peer_port: {
                    DelayedRecovery.DELAYED_RECOVERY_STATE: STATE_DISABLED,
                    DelayedRecovery.DELAYED_RECOVERY_LOSS_TH: CUSTOM_LOSS_TH_B,
                    DelayedRecovery.DELAYED_RECOVERY_RETRY_TH: CUSTOM_RETRY_TH_B,
                },
            },
            {
                selected_port: delayed_recovery_expected(
                    admin_values(
                        state=STATE_ENABLED,
                        loss_th=CUSTOM_LOSS_TH_A,
                        retry_th=CUSTOM_RETRY_TH_A,
                    ),
                    oper_values(
                        state=STATE_ENABLED,
                        loss_th=CUSTOM_LOSS_TH_A,
                        retry_th=CUSTOM_RETRY_TH_A,
                    ),
                ),
                selected_peer_port: delayed_recovery_expected(
                    admin_values(
                        state=STATE_DISABLED,
                        loss_th=CUSTOM_LOSS_TH_B,
                        retry_th=CUSTOM_RETRY_TH_B,
                    ),
                    oper_values(
                        state=STATE_DISABLED,
                        loss_th=CUSTOM_LOSS_TH_B,
                        retry_th=CUSTOM_RETRY_TH_B,
                    ),
                ),
            },
        ),
    ]

    with allure.step("Validate switch-switch delayed recovery with no force applied"):
        for scenario_name, configuration, expected_values in no_force_scenarios:
            with allure.independent_step(scenario_name):
                # Clear and configure both ports in a single revision, so one apply
                # flaps the link once and both ends renegotiate together. Applying the
                # unset separately would flap twice per scenario and leave a window
                # where the two ends are configured differently.
                unset_delayed_recovery(delayed_recovery_ports, apply=False)
                for fae_port, delayed_recovery_values in configuration.items():
                    set_delayed_recovery_values(fae_port, delayed_recovery_values)
                apply_config()
                wait_for_delayed_recovery_ports(delayed_recovery_ports)
                for fae_port, expected_value in expected_values.items():
                    validate_expected_values_by_rev(fae_port, expected_value)


@pytest.mark.phy
def test_delayed_recovery_forced_values(engines, devices, topology_obj, register_cleanup,
                                        delayed_recovery_ports, disable_logic_relock):
    """
    Test flow:
        1. pick connected switch-switch ports
        2. enable force on one side and disable force on the peer
        3. verify forced operational values on both ports
    """
    with allure.step("get connected ports (peer ports)"):
        selected_port, selected_peer_port = delayed_recovery_ports
        register_cleanup(lambda: unset_delayed_recovery(delayed_recovery_ports))

    # Side A's resolved default is what gets forced onto the peer, so both expectations
    # below derive from side A's module type.
    enabled_loss_th = get_enabled_loss_th(selected_port)

    force_all_values = force_values(
        state_force=FORCE_ENABLED,
        loss_th_force=FORCE_ENABLED,
        retry_th_force=FORCE_ENABLED,
    )
    peer_force_disabled_values = force_values(
        state_force=FORCE_DISABLED,
        loss_th_force=FORCE_DISABLED,
        retry_th_force=FORCE_DISABLED,
    )

    forced_scenarios = [
        (
            "Force default values",
            {
                selected_port: force_all_values,
                selected_peer_port: peer_force_disabled_values,
            },
            {
                selected_port: delayed_recovery_expected(
                    admin_values(loss_th_force=FORCE_ENABLED, retry_th_force=FORCE_ENABLED),
                    oper_values(
                        loss_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_LOSS_TH"],
                        retry_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_RETRY_TH"]
                    ),
                ),
                selected_peer_port: delayed_recovery_expected(
                    admin_values(
                        state_force=FORCE_DISABLED,
                        loss_th_force=FORCE_DISABLED,
                        retry_th_force=FORCE_DISABLED,
                    ),
                    oper_values(
                        loss_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_LOSS_TH"],
                        retry_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_RETRY_TH"]
                    ),
                ),
            },
        ),
        (
            "Force enabled state on disabled peer",
            {
                selected_port: {
                    **force_all_values,
                    DelayedRecovery.DELAYED_RECOVERY_STATE: STATE_ENABLED,
                },
                selected_peer_port: {
                    **peer_force_disabled_values,
                    DelayedRecovery.DELAYED_RECOVERY_STATE: STATE_DISABLED,
                },
            },
            {
                selected_port: delayed_recovery_expected(
                    admin_values(
                        state=STATE_ENABLED,
                        loss_th_force=FORCE_ENABLED,
                        retry_th_force=FORCE_ENABLED,
                    ),
                    oper_values(
                        state=STATE_ENABLED,
                        loss_th=enabled_loss_th,
                        retry_th=OPER_ENABLED_RETRY_TH,
                    ),
                ),
                selected_peer_port: delayed_recovery_expected(
                    admin_values(
                        state=STATE_DISABLED,
                        state_force=FORCE_DISABLED,
                        loss_th_force=FORCE_DISABLED,
                        retry_th_force=FORCE_DISABLED,
                    ),
                    oper_values(
                        state=STATE_ENABLED,
                        loss_th=enabled_loss_th,
                        retry_th=OPER_ENABLED_RETRY_TH,
                    ),
                ),
            },
        ),
        (
            "Force disabled state on enabled peer",
            {
                selected_port: {
                    **force_all_values,
                    DelayedRecovery.DELAYED_RECOVERY_STATE: STATE_DISABLED,
                },
                selected_peer_port: {
                    **peer_force_disabled_values,
                    DelayedRecovery.DELAYED_RECOVERY_STATE: STATE_ENABLED,
                },
            },
            {
                selected_port: delayed_recovery_expected(
                    admin_values(
                        state=STATE_DISABLED,
                        loss_th_force=FORCE_ENABLED,
                        retry_th_force=FORCE_ENABLED,
                    ),
                    oper_values(
                        loss_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_LOSS_TH"],
                        retry_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_RETRY_TH"]
                    ),
                ),
                selected_peer_port: delayed_recovery_expected(
                    admin_values(
                        state=STATE_ENABLED,
                        state_force=FORCE_DISABLED,
                        loss_th_force=FORCE_DISABLED,
                        retry_th_force=FORCE_DISABLED,
                    ),
                    oper_values(
                        loss_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_LOSS_TH"],
                        retry_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_RETRY_TH"]
                    ),
                ),
            },
        ),
        (
            "Force custom thresholds while peer has different thresholds",
            {
                selected_port: {
                    **force_all_values,
                    DelayedRecovery.DELAYED_RECOVERY_LOSS_TH: CUSTOM_LOSS_TH_A,
                    DelayedRecovery.DELAYED_RECOVERY_RETRY_TH: CUSTOM_RETRY_TH_A,
                },
                selected_peer_port: {
                    **peer_force_disabled_values,
                    DelayedRecovery.DELAYED_RECOVERY_LOSS_TH: CUSTOM_LOSS_TH_B,
                    DelayedRecovery.DELAYED_RECOVERY_RETRY_TH: CUSTOM_RETRY_TH_B,
                },
            },
            {
                selected_port: delayed_recovery_expected(
                    admin_values(
                        loss_th=CUSTOM_LOSS_TH_A,
                        retry_th=CUSTOM_RETRY_TH_A,
                        loss_th_force=FORCE_ENABLED,
                        retry_th_force=FORCE_ENABLED,
                    ),
                    oper_values(
                        loss_th=CUSTOM_LOSS_TH_A,
                        retry_th=CUSTOM_RETRY_TH_A,
                    ),
                ),
                selected_peer_port: delayed_recovery_expected(
                    admin_values(
                        loss_th=CUSTOM_LOSS_TH_B,
                        retry_th=CUSTOM_RETRY_TH_B,
                        state_force=FORCE_DISABLED,
                        loss_th_force=FORCE_DISABLED,
                        retry_th_force=FORCE_DISABLED,
                    ),
                    oper_values(
                        loss_th=CUSTOM_LOSS_TH_A,
                        retry_th=CUSTOM_RETRY_TH_A,
                    ),
                ),
            },
        ),
        (
            "Force retry threshold while peer has different retry threshold",
            {
                selected_port: {
                    **force_values(retry_th_force=FORCE_ENABLED),
                    DelayedRecovery.DELAYED_RECOVERY_RETRY_TH: CUSTOM_RETRY_TH_A,
                },
                selected_peer_port: {
                    **force_values(retry_th_force=FORCE_DISABLED),
                    DelayedRecovery.DELAYED_RECOVERY_RETRY_TH: CUSTOM_RETRY_TH_B,
                },
            },
            {
                selected_port: delayed_recovery_expected(
                    admin_values(
                        retry_th=CUSTOM_RETRY_TH_A,
                        retry_th_force=FORCE_ENABLED,
                    ),
                    oper_values(
                        loss_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_LOSS_TH"],
                        retry_th=CUSTOM_RETRY_TH_A
                    ),
                ),
                selected_peer_port: delayed_recovery_expected(
                    admin_values(
                        retry_th=CUSTOM_RETRY_TH_B,
                        retry_th_force=FORCE_DISABLED,
                    ),
                    oper_values(
                        loss_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_LOSS_TH"],
                        retry_th=CUSTOM_RETRY_TH_A
                    )
                ),
            },
        ),
        (
            "Force loss threshold while peer has different loss threshold",
            {
                selected_port: {
                    **force_values(loss_th_force=FORCE_ENABLED),
                    DelayedRecovery.DELAYED_RECOVERY_LOSS_TH: CUSTOM_LOSS_TH_A,
                },
                selected_peer_port: {
                    **force_values(loss_th_force=FORCE_DISABLED),
                    DelayedRecovery.DELAYED_RECOVERY_LOSS_TH: CUSTOM_LOSS_TH_B,
                },
            },
            {
                selected_port: delayed_recovery_expected(
                    admin_values(
                        loss_th=CUSTOM_LOSS_TH_A,
                        loss_th_force=FORCE_ENABLED,
                    ),
                    oper_values(
                        loss_th=CUSTOM_LOSS_TH_A,
                        retry_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_RETRY_TH"]
                    )
                ),
                selected_peer_port: delayed_recovery_expected(
                    admin_values(
                        loss_th=CUSTOM_LOSS_TH_B,
                        loss_th_force=FORCE_DISABLED,
                    ),
                    oper_values(
                        loss_th=CUSTOM_LOSS_TH_A,
                        retry_th=devices.dut.default_delayed_recovery_values["OPER_DISABLED_RETRY_TH"],
                    ),
                ),
            },
        ),
    ]

    with allure.step("Validate switch-switch delayed recovery with force applied"):
        for scenario_name, configuration, expected_values in forced_scenarios:
            with allure.independent_step(scenario_name):
                # Single revision for both ports - see the no-force loop above.
                unset_delayed_recovery(delayed_recovery_ports, apply=False)
                for fae_port, delayed_recovery_values in configuration.items():
                    set_delayed_recovery_values(fae_port, delayed_recovery_values)
                apply_config()
                wait_for_delayed_recovery_ports(delayed_recovery_ports)
                for fae_port, expected_value in expected_values.items():
                    validate_expected_values_by_rev(fae_port, expected_value)
