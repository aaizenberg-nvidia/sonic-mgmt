import logging
import random
import time

import pytest
from retry.api import retry_call

from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import ApiType, LogsSources
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import FWRecoveryConsts, NvosConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.cluster.cluster_tools import summarize_switch_ports
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.interfaces.nvl5_port.helpers import (
    reset_gpus_if_needed,
    select_random_nvl_port_name,
    skip_if_no_access_links,
    skip_if_no_trunk_links,
    validate_default_config,
    validate_mode_set,
)
from ngts.tests_nvos.system.gnmi.constants import GnmicErr, GnmiMode
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.helpers import verify_msg_in_out_or_err, verify_msg_not_in_out_or_err
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()

# Constants
DEFAULT_GPU_TIMEOUT = 100  # GPU default timeout in seconds for non-standalone systems

# Shared config templates
FAE_RECOVERY_CONFIG_DICT = {
    "serdes-eq": {
        "setting_prefix": FWRecoveryConsts.SerdesEQ.SETTING_PREFIX,
        "mode": FWRecoveryConsts.SerdesEQ.MODE,
        "timeout": FWRecoveryConsts.SerdesEQ.TIMEOUT,
    },
    # "logic-relock": {
    #     "setting_prefix": FWRecoveryConsts.LogicRelock.SETTING_PREFIX,
    #     "mode": FWRecoveryConsts.LogicRelock.MODE,
    #     "timeout": FWRecoveryConsts.LogicRelock.TIMEOUT,
    # }
}

FAE_RECOVERY_CONFIG_PARAMS = [pytest.param(config, id=name) for name, config in FAE_RECOVERY_CONFIG_DICT.items()]


@pytest.mark.interface
@pytest.mark.multiplanar
def test_show_fw_recovery_counters(engines, devices):
    """
    @summary:
        Verify default FAE recovery counters via NV "show interface" and GNMI subscription.

    Steps:
    1. Run `nv show interface <port> link phy detail --view detailed` and parse JSON output.
    2. Confirm all default counters match FWRecoveryConsts.DEFAULT_FW_RECOVERY_COUNTERS.
    3. Pick a random counter, subscribe via GNMI ONCE to `phy-diag/state/<counter>`.
    4. Verify GNMI stream contains the counter name and its default value.
    """
    port_result = RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_ALL_TYPES)

    if not port_result.result:
        pytest.skip(f"Skipping test - {port_result.info}")

    selected_port = port_result.get_returned_value()

    with allure.step("Validate show interface command with all nvl5 interfaces"):
        port_output = OutputParsingTool.parse_json_str_to_dictionary(selected_port.interface.link.phy.detail.show()).get_returned_value()

    expected_fields = list(FWRecoveryConsts.DEFAULT_FW_RECOVERY_COUNTERS.keys())
    expected_values = list(FWRecoveryConsts.DEFAULT_FW_RECOVERY_COUNTERS.values())

    ValidationTool.validate_fields_values_in_output(expected_fields, expected_values, port_output)

    random_counter = random.choice(expected_fields)
    with allure.step("set up gnmi client"):
        client = GnmiClient(
            engines.dut.ip,
            GnmiConsts.GNMI_DEFAULT_PORT,
            devices.dut.default_username,
            devices.dut.default_password,
            verify_tools_installed=True,
        )

    with allure.step(f"subscribe client to counter: {random_counter} and verify information"):
        out, err = client.gnmic_subscribe_interface(
            GnmiMode.ONCE, selected_port.name, skip_cert_verify=True, interface_path=f"phy-diag/state/{random_counter}"
        )

    with allure.step(f'check that "{random_counter}" was streamed'):
        verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, out, err)
        verify_msg_in_out_or_err(f"{random_counter}: {FWRecoveryConsts.DEFAULT_FW_RECOVERY_COUNTERS.get(random_counter)}", out)


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.parametrize("test_api", random.sample(ApiType.ALL_TYPES, 1))
def test_fw_recovery_bad_flow(devices, engines, test_name, test_api):
    """
    @summary:
        Negative validation of phy-recovery parameters rejects invalid values.

    Steps:
    1. Attempt to show a non-existent phy-recovery attribute; expect failure.
    2. For serdes-eq-mode and serdes-eq-timeout:
       a. Set the field to its bad_value with apply=True and ask_for_confirmation=True.
       b. Verify that the expected_error is raised.
    """
    TestToolkit.tested_api = test_api
    with allure.step("Select port for test"):
        port_result = RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_ALL_TYPES)

        if not port_result.result:
            pytest.skip(f"Skipping test - {port_result.info}")

        selected_port = port_result.get_returned_value()
        selected_port = Fae(port_name=selected_port.name)

    with allure.step(f"Testing interface {selected_port.port.name}"):
        phy_recovery_obj = selected_port.port.interface.link.phy_recovery
        with allure.independent_step("Testing show non-existing attribute in phy-recovery"):
            phy_recovery_obj.show("non-existing", should_succeed=False)

        with allure.independent_step(f"Testing bad-mode on interface {selected_port.port.name}"):
            logger.info(f"Set {FWRecoveryConsts.SerdesEQ.MODE} to bad-mode")
            phy_recovery_obj.set(
                FWRecoveryConsts.SerdesEQ.MODE, "bad-mode", apply=True, ask_for_confirmation=True, expected_str="'bad-mode' is not one of"
            ).verify_result()

        with allure.independent_step(f"Testing bad-timeout on interface {selected_port.port.name}"):
            logger.info(f"Set {FWRecoveryConsts.SerdesEQ.TIMEOUT} to -1")
            expected_str = (
                "-1 is less than the minimum of 0"
                if is_bug_active(4631963) and test_api == ApiType.OPENAPI
                else "Valid range for serdes-eq-timeout is 0 - 2550"
            )
            phy_recovery_obj.set(
                FWRecoveryConsts.SerdesEQ.TIMEOUT, -1, apply=True, ask_for_confirmation=True, expected_str=expected_str
            ).verify_result()


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.parametrize("config", FAE_RECOVERY_CONFIG_PARAMS)
def test_set_fae_fw_recovery_trunk_ports(engines, devices, random_api, standalone_system: bool, has_loopbox: bool, config):
    """
    @summary:
        Verify that firmware recovery settings (mode and timeout) can be applied and updated on trunk ports (sw).

    Steps:
    1. Skip if no trunk links.
    2. Enable recovery mode and set timeout on all trunk ports.
    3. Verify settings on a single random trunk port.
    4. Update timeout higher and lower, verifying group vs. local effects.
    5. Disable recovery and confirm defaults restored.
    """
    if not has_loopbox:
        pytest.skip("Skipping test on standalone system without loopbox (no links in up state)")

    skip_if_no_trunk_links(devices)
    port_name = select_random_nvl_port_name(devices, "sw")
    summarized_switch_ports = summarize_switch_ports(devices.dut.nvl_trunk_ports_list)

    _run_fae_mode_timeout_test(
        test_api=random_api,
        group_all_ports=summarized_switch_ports,
        devices=devices,
        port_name=port_name,
        config=config,
    )


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.parametrize("config", FAE_RECOVERY_CONFIG_PARAMS)
def test_set_fae_fw_recovery_access_ports(engines, devices, random_api, standalone_system, has_loopbox, config, setup_name):
    """
    @summary:
        Verify that firmware recovery settings (mode and timeout) can be applied and updated on access ports (acp).

    Steps:
    1. Skip if no access links.
    2. Enable recovery mode and set timeout on all access ports.
    3. Verify settings on a single random access port.
    4. Update timeout higher and lower, verifying group vs. local effects.
    5. Disable recovery and confirm defaults restored.
    """
    skip_if_no_access_links(has_loopbox, standalone_system)
    port_name = select_random_nvl_port_name(devices, "acp")
    _run_fae_mode_timeout_test(
        test_api=random_api,
        group_all_ports=f"acp1-{str(len(devices.dut.nvl_access_ports_list))}",
        devices=devices,
        port_name=port_name,
        config=config,
        standalone_system=standalone_system,
        setup_name=setup_name,
    )


def _run_fae_mode_timeout_test(test_api, group_all_ports, devices, port_name, config, standalone_system=False, setup_name=None):
    """
    Test firmware recovery timeout behavior for both standalone and non-standalone systems.

    In non-standalone systems, timeout values are negotiated between switch and GPU.
    The system uses MAX(configured_timeout, gpu_default_timeout) due to GPU constraints
    that enforce a minimum timeout of 100 seconds.

    Args:
        standalone_system: If True, configured timeouts are used directly.
                          If False, timeouts are negotiated with GPU (MAX logic applies).
    """
    TestToolkit.tested_api = test_api

    selected_port = Fae(port_name=port_name)
    all_ports = Fae(port_name=group_all_ports)

    validate_default_config(selected_port)

    def _get_expected_timeout(configured_timeout, is_standalone):
        """Calculate expected timeout based on system type and GPU negotiation."""
        return configured_timeout if is_standalone else max(configured_timeout, DEFAULT_GPU_TIMEOUT)

    try:
        test_mods: list[str] = FWRecoveryConsts.MODES.copy()
        if is_bug_active(4682685):
            test_mods.remove(FWRecoveryConsts.ENABLED)
        for mode in test_mods:
            with allure.step(f"Set {config['mode']} to {mode}"):
                if mode == FWRecoveryConsts.ENABLED:
                    _apply_mode(all_ports, config, mode)
                    _apply_timeout(all_ports, config, 100)
                    reset_gpus_if_needed(setup_name)
                    _verify_config(selected_port, config, mode, 100)

                    higher_timeout = random.randint(11, 20) * 10
                    with allure.step(f"Update timeout to higher value ({higher_timeout}) while mode {mode}"):
                        _apply_timeout(all_ports, config, higher_timeout)
                        reset_gpus_if_needed(setup_name)
                        _validate_timeout(selected_port, config, higher_timeout)

                    lower_timeout = random.randint(1, 9) * 10

                    if standalone_system:
                        with allure.step(f"Update timeout to lower value ({lower_timeout}) locally — expect NO change"):
                            _apply_timeout(selected_port, config, lower_timeout)
                            # Standalone: local changes don't propagate, keep higher timeout
                            _validate_timeout(selected_port, config, higher_timeout, verify_after_seconds=30)
                    else:
                        with allure.step(
                            f"Update timeout to lower value ({lower_timeout}) locally — expect GPU default ({DEFAULT_GPU_TIMEOUT})"
                        ):
                            _apply_timeout(selected_port, config, lower_timeout)
                            reset_gpus_if_needed(setup_name)
                            # Non-standalone: GPU negotiates, expects MAX(lower_timeout, GPU_default) = 100
                            expected_timeout = max(lower_timeout, DEFAULT_GPU_TIMEOUT)
                            _validate_timeout(selected_port, config, expected_timeout, verify_after_seconds=30)

                    with allure.step(f"Update timeout to lower value ({lower_timeout}) on all ports"):
                        _apply_timeout(all_ports, config, lower_timeout)
                        reset_gpus_if_needed(setup_name)
                        # All ports change: calculate expected timeout for this scenario
                        expected_timeout = _get_expected_timeout(lower_timeout, standalone_system)
                        _validate_timeout(selected_port, config, expected_timeout)
                else:
                    _apply_mode(all_ports, config, mode)
                    _verify_config(selected_port, config, mode)
    finally:
        with allure.step("Unset configuration to return to defaults"):
            all_ports.port.interface.link.phy_recovery.unset(apply=True, ask_for_confirmation=True).verify_result()
            reset_gpus_if_needed(setup_name)

        retry_call(validate_default_config, [selected_port], exceptions=AssertionError, tries=6, delay=30)


def _apply_mode(selected_port, config, mode):
    selected_port.port.interface.link.phy_recovery.set(config["mode"], mode, apply=True, ask_for_confirmation=True).verify_result()


def _apply_timeout(selected_port, config, timeout):
    selected_port.port.interface.link.phy_recovery.set(config["timeout"], timeout, apply=True, ask_for_confirmation=True).verify_result()


def _verify_config(selected_port, config, mode, timeout=None):
    retry_call(validate_mode_set, [selected_port, config, mode, timeout], exceptions=AssertionError, tries=6, delay=30)


def _validate_timeout(selected_port, config, expected_timeout=None, verify_after_seconds=None):
    _verify_config(selected_port, config, FWRecoveryConsts.ENABLED, expected_timeout)

    if verify_after_seconds:
        with allure.step(f"Wait {verify_after_seconds}s and re-verify timeout is still {expected_timeout}"):
            time.sleep(verify_after_seconds)
            _verify_config(selected_port, config, FWRecoveryConsts.ENABLED, expected_timeout)


@pytest.mark.interface
@pytest.mark.multiplanar
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_phy_recovery_go_once(engines, devices, test_api):
    """
    @summary:
        Verify the go once phy-recovery action (nv action start fae int <port> link phy-recovery).
        Tests both negative flows (invalid port types) and positive flow (sw and acp ports).

    Steps:
    1. Bad flow: Attempt to run the action on mgmt ports (eth0), fnm ports (fnm1), and loopback (lo).
       Verify that each of these fails with an appropriate error message.
    2. Good flow: Run the action on a random trunk (sw) port and verify it succeeds.
    3. Good flow: Run the action on an acp port and verify it succeeds.
    4. Verify the expected log message appears in syslog.
    """
    system = System()
    TestToolkit.tested_api = test_api

    with allure.step("Bad flow: Verify action fails on invalid port types"):
        with allure.independent_step("Test invalid port types (mgmt, fnm, lo)"):
            for invalid_port in FWRecoveryConsts.GO_ONCE_INVALID_PORT_TYPES:
                with allure.step(f"Test action start phy-recovery on invalid port: {invalid_port}"):
                    fae_port = Fae(port_name=invalid_port)
                    fae_port.interface.link.phy_recovery.action_start_go_once().verify_result(False)

    tested_ports = []
    with allure.step("Good flow: Run action start phy-recovery on valid ports"):
        for interface_type in ["sw", "acp"]:
            with allure.independent_step(f"Test {interface_type} port"):
                port_result = RandomizationTool.select_random_port(
                    requested_ports_state=NvosConsts.LINK_STATE_ALL_TYPES, interface_type=interface_type
                )
                if not port_result.result:
                    logger.info(f"Skipping {interface_type} port test - {port_result.info}")
                    port_result.ignore_result()
                    continue
                port = port_result.get_returned_value()
                logger.info(f"Selected {interface_type} port for go once test: {port.name}")
                fae_port = Fae(port_name=port.name)
                fae_port.interface.link.phy_recovery.action_start_go_once().verify_result()
                tested_ports.append(interface_type)

    if tested_ports:
        with allure.step(f"Verify expected log message '{FWRecoveryConsts.GO_ONCE_LOG_MESSAGE}' in syslog"):
            TestToolkit.tested_api = ApiType.NVUE
            system.log.verify_expected_logs(
                logs_to_find=[FWRecoveryConsts.GO_ONCE_LOG_MESSAGE],
                logs_source=LogsSources.SYSLOG,
                engine=engines.dut,
                only_latest_log=True,
            )
    else:
        logger.info("Skipping log verification - no valid sw or acp ports were tested")
