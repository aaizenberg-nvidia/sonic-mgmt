import logging
import time
import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import NvosConst, SystemConsts, ConfState
from ngts.nvos_tools.infra import ExceptionTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, wait_until_cli_is_up
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.FilesTool import FilesTool
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.constants.constants import LinuxConsts
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RegressionConfigurations import RegressionConfigurations
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.authentication_restrictions.constants import RestrictionsConsts
from ngts.tests_nvos.system.clock.ClockConsts import ClockConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.tests_nvos.general.security.security_test_tools.security_test_utils import (
    get_fips_state,
    switch_fips_mode
)


def _should_use_new_auth_format(dut_engine):
    """
    Determine if we should use the new authentication order format
    by delegating to the device's _should_use_new_format method.

    Args:
        dut_engine: SSH engine for the DUT

    Returns:
        bool: True if new format should be used, False for legacy format
    """
    with allure.step('Check if should use new auth format'):
        if not TestToolkit.is_eth_dut():
            return False  # NVOS devices use different format

        try:
            # Delegate to the device's _should_use_new_format method
            if hasattr(TestToolkit, 'devices') and TestToolkit.devices and hasattr(TestToolkit.devices, 'dut'):
                return TestToolkit.devices.dut._should_use_new_format(dut_engine)
            else:
                logging.warning('Cleanup: TestToolkit.devices.dut not available, using LEGACY format')
                return False
        except Exception as e:
            logging.warning(f'Cleanup: Exception during format check - using LEGACY format: {e}')
            return False


def set_base_configurations_cl(dut_engine, timezone=LinuxConsts.ETC_UTC_TIMEZONE, apply=False, save_conf=False):
    """
    @summary: Set base configurations.
        Used in:
            - nvos post installation steps
            - nvos clear config (post test) function
    """
    with allure.step('Set base configurations CL'):
        logging.info('Set base configurations')
        orig_api = TestToolkit.tested_api

        try:
            logging.info('Change tested api to NVUE')
            TestToolkit.tested_api = ApiType.NVUE

            logging.info(f'Set switch timezone: {timezone}')
            system = System()
            system.datetime.set(ClockConsts.TIMEZONE, LinuxConsts.ETC_UTC_TIMEZONE, dut_engine=dut_engine).verify_result()
            system.api.set(SystemConsts.STATE, NvosConst.ENABLED, dut_engine=dut_engine).verify_result()

            if apply:
                logging.info('Apply configurations')
                NvueGeneralCli.apply_config(engine=dut_engine, option='--assume-yes')

            if save_conf:
                logging.info('Save configurations')
                NvueGeneralCli.save_config(dut_engine)
        finally:
            logging.info(f'Change tested api back to {orig_api}')
            TestToolkit.tested_api = orig_api


def set_base_configurations(dut_engine, timezone=LinuxConsts.JERUSALEM_TIMEZONE, apply=False, save_conf=False):
    """
    @summary: Set base configurations.
        Used in:
            - nvos post installation steps
            - nvos clear config (post test) function
    """
    with allure.step('Set base configurations'):
        logging.info('Set base configurations')
        orig_api = TestToolkit.tested_api

        try:
            logging.info('Change tested api to NVUE')
            TestToolkit.tested_api = ApiType.NVUE

            logging.info(f'Set switch timezone: {timezone}')
            system = System()
            system.datetime.set(ClockConsts.TIMEZONE, LinuxConsts.JERUSALEM_TIMEZONE, dut_engine=dut_engine).verify_result()

            logging.info('Set authentication restrictions configurations')
            system.aaa.authentication.restrictions.set(RestrictionsConsts.LOCKOUT_STATE,
                                                       RestrictionsConsts.DISABLED, dut_engine=dut_engine).verify_result()
            system.aaa.authentication.restrictions.set(RestrictionsConsts.FAIL_DELAY, 0,
                                                       dut_engine=dut_engine).verify_result()

            RegressionConfigurations.set_base_configurations(engine=dut_engine, apply=False)

            if apply:
                logging.info('Apply configurations')
                NvueGeneralCli.apply_config(engine=dut_engine, option='-y', verify_execution=True)

            if save_conf:
                logging.info('Save configurations')
                NvueGeneralCli.save_config(dut_engine)
        finally:
            logging.info(f'Change tested api back to {orig_api}')
            TestToolkit.tested_api = orig_api


def clear_conf(engine, device, config_yml, root_dir, version=""):
    try:
        if not config_yml or not FilesTool.file_exists(engine, config_yml):
            with allure.step("Config file is empty or can't be found. Trying to copy default yml again"):
                config_yml = device.get_default_config_yml(engine, root_dir, version=version)

        if config_yml:
            with allure.step("Replace config"):
                with allure.step("Replace action"):
                    allure.attach("Selected config yml", config_yml)
                    output = NvueGeneralCli.replace_config(engine, config_yml)
                    assert "Error" not in output, "Failed to replace config"
                with allure.step("Config diff"):
                    output = NvueGeneralCli.diff_config(engine)
                    if output:
                        allure.attach("Config diff", output)
                        with allure.step("Apply config"):
                            output = NvueGeneralCli.apply_config(engine=engine, option='-y', verify_execution=True)
                            allure.attach("Apply output", output)
                            assert ConfState.APPLIED in output, "Failed to apply config"
                            time.sleep(5)
                        with allure.step("Save config"):
                            output = NvueGeneralCli.save_config(engine)
                            allure.attach("Save output", output)
                            assert ConfState.SAVED in output, "Failed to save config"
                    else:
                        with allure.step("Config diff is empty, no need to apply and save (detaching config)"):
                            NvueGeneralCli.detach_config(engine)
        else:
            with allure.step("Clear config using unset commands"):
                clear_config_using_unset(engine, device)

    except BaseException as ex:
        allure.attach("Exception", ExceptionTool.format_traceback())
        logging.error(f"Replace config failed - {ExceptionTool.format_exception(ex)}")
        raise


def wait_for_rsyslog_reconfiguration(engine, timeout=10):
    """
    @summary: A config apply/save can make hostcfgd regenerate rsyslog's config, which briefly
        restarts rsyslog.service. Wait until that settles so callers don't race a status check
        (e.g. loganalyzer) against the restart.
    """
    for _ in range(int(timeout * 2)):
        status = engine.run_cmd('systemctl is-active rsyslog-config.service', validate=False)
        if status.strip() not in ('active', 'activating'):
            return
        time.sleep(0.5)


def clear_config_using_unset(engine, device):
    try:
        with allure.step("Detach config"):
            NvueGeneralCli.detach_config(engine)

        with allure.step("Unset all"):
            engine.run_cmd(device.unset_all_command)
            allure.attach("Unset command", device.unset_all_command)

            with allure.step("Config diff"):
                output = NvueGeneralCli.diff_config(engine)
                if output:
                    allure.attach("Config diff", output)
                    with allure.step("Set base configurations"):
                        set_base_configurations(dut_engine=engine, apply=True, save_conf=True)
                        wait_for_rsyslog_reconfiguration(engine)

    except BaseException as ex:
        allure.attach("Exception", str(ex))
        logging.error(f"Failed to clear config - {ex}")


def clear_cl_conf(dut_engine, markers=None, dut=None):
    if markers and 'system_profile_cleanup' in markers:
        clear_system_profile_config()

    with allure.step("Detach config"):
        NvueGeneralCli.detach_config(dut_engine)

    with allure.step("Get a list of 'set' components"):
        show_config_output = OutputParsingTool.parse_json_str_to_dictionary(
            NvueGeneralCli.show_config(dut_engine)).get_returned_value()

        set_comp = {k: v for comp in show_config_output for k, v in comp.get("set", {}).items()}

        with allure.step("Get the non-default set components"):
            default_conf = dut.get_default_nvue_config()
            diff_config = ValidationTool.get_dictionaries_diff(set_comp, default_conf)
            logging.info(diff_config)

        if diff_config:
            active_port = None
            if NvosConst.INTERFACE in diff_config.keys():
                result = RandomizationTool.select_random_ports(
                    num_of_ports_to_select=1, dut_engine=dut_engine
                )
                if result.result:
                    active_port = result.returned_value[-1]
                else:
                    # No swp port in `up` state is fine here — `active_port` is
                    # only used at the end of cleanup to wait for the chosen
                    # port to recover. If there is no such port (e.g. on a
                    # Cumulus DUT where all swp ports are admin-down), this
                    # cleanup step is a no-op. We must still consume the
                    # ResultObj so the autouse `verify_result_objects` fixture
                    # does not trip in a later test's teardown.
                    result.ignore_result()

                unset_iface_cli = "nv unset interface"
                unset_iface_cmd = ""
                iface_config = diff_config.get(NvosConst.INTERFACE, {})
                unset_iface_cmd += " ".join([f"{unset_iface_cli} {iface_name}; " for iface_name in
                                             iface_config.keys()])

                logging.info("Execute interface unset commands")
                dut_engine.run_cmd(unset_iface_cmd)

            # Clean up non-default VRFs
            if 'vrf' in diff_config.keys():
                unset_vrf_cli = "nv unset vrf"
                unset_vrf_cmd = ""
                vrf_config = diff_config.get('vrf', {})
                # Unset all non-default VRFs (exclude mgmt VRF which is default)
                unset_vrf_cmd += " ".join([f"{unset_vrf_cli} {vrf_name}; " for vrf_name in
                                          vrf_config.keys() if vrf_name.lower() not in ['mgmt', 'default']])

                if unset_vrf_cmd:
                    logging.info("Execute VRF unset commands")
                    dut_engine.run_cmd(unset_vrf_cmd)

            should_wait_for_nvued_after_apply = False

            if NvosConst.SYSTEM in diff_config.keys():
                # CRITICAL: Check if FIPS is enabled and handle it first
                system_config = diff_config.get(NvosConst.SYSTEM, {})
                has_security_changes = 'security' in system_config
                has_aaa_changes = NvosConst.SYSTEM_AAA in system_config

                if has_security_changes or has_aaa_changes:
                    with allure.step("Check FIPS mode before cleanup"):
                        try:
                            # Create engines-like object for FIPS functions
                            # The FIPS functions expect an engines object with .dut attribute
                            class EnginesWrapper:
                                def __init__(self, dut_engine):
                                    self.dut = dut_engine

                            engines_wrapper = EnginesWrapper(dut_engine)

                            # Get current FIPS state using the shared function
                            fips_state = get_fips_state(engines_wrapper)
                            is_fips_enabled = fips_state.get('operational', '') == 'enabled'
                            logging.info(f"Current FIPS state before cleanup: {fips_state}, is_enabled: {is_fips_enabled}")

                            if is_fips_enabled:
                                # Disable FIPS mode using the shared function
                                logging.info("FIPS mode detected, disabling with reboot before AAA cleanup")
                                switch_fips_mode(engines_wrapper, on=False, should_reboot=True)
                                logging.info("FIPS disabled and system rebooted successfully")
                        except Exception as e:
                            logging.warning(f"Failed to check/disable FIPS mode during cleanup: {e}")

                with allure.step("Unset each system 'set' command"):
                    unset_system_cli = "nv unset system"
                    should_wait_for_nvued_after_apply = NvosConst.SYSTEM_AAA in diff_config[
                        NvosConst.SYSTEM].keys() \
                        and NvosConst.SYSTEM_AUTHENTICATION in \
                        diff_config[NvosConst.SYSTEM][
                        NvosConst.SYSTEM_AAA].keys() \
                        and NvosConst.SYSTEM_AUTHENTICATION_ORDER in \
                        diff_config[NvosConst.SYSTEM][NvosConst.SYSTEM_AAA][
                        NvosConst.SYSTEM_AUTHENTICATION].keys()

                    unset_cli_cmd = ""

                    aaa_config = system_config.get(NvosConst.SYSTEM_AAA, {})
                    user_config = aaa_config.get(NvosConst.SYSTEM_AAA_USER, {})

                    # unset system user for non-default users
                    unset_cli_cmd += " ".join([f"{unset_system_cli} {NvosConst.SYSTEM_AAA} "
                                               f"{NvosConst.SYSTEM_AAA_USER} {user_comp}; " for user_comp in
                                               user_config.keys() if
                                               user_comp != NvosConst.SYSTEM_AAA_USER_ADMIN and
                                               user_comp != NvosConst.SYSTEM_AAA_USER_MONITOR and
                                               user_comp != NvosConst.SYSTEM_AAA_USER_CUMULUS])

                    # NVUE cannot remove users that are logged on, so kill any users that will be removed
                    for user in user_config.keys():
                        if user != NvosConst.SYSTEM_AAA_USER_ADMIN and \
                                user != NvosConst.SYSTEM_AAA_USER_MONITOR and \
                                user != NvosConst.SYSTEM_AAA_USER_CUMULUS:
                            logging.info(f"killing user: {user}")
                            dut_engine.run_cmd(f'sudo pkill -9 -u {user}')

                    # STEP 1: Unset authentication order FIRST (if it exists) and apply separately
                    # This is critical for FIPS mode compatibility
                    has_auth_order = (NvosConst.SYSTEM_AUTHENTICATION in aaa_config or
                                      "authentication-order" in aaa_config)

                    if has_auth_order:
                        with allure.step("Unset authentication order FIRST (before other AAA components)"):
                            auth_order_cmd = ""
                            if _should_use_new_auth_format(dut_engine):
                                # New format (>= 5.15.0): nv unset system aaa authentication
                                auth_order_cmd = f"{unset_system_cli} {NvosConst.SYSTEM_AAA} {NvosConst.SYSTEM_AUTHENTICATION}"
                            else:
                                # Legacy format (< 5.15.0): nv unset system aaa authentication-order
                                auth_order_cmd = f"{unset_system_cli} {NvosConst.SYSTEM_AAA} authentication-order"

                            logging.info(f"Unsetting authentication order FIRST: {auth_order_cmd}")
                            dut_engine.run_cmd(auth_order_cmd)

                            # Apply auth order unset separately
                            logging.info("Applying authentication order unset")
                            NvueGeneralCli.apply_config(dut_engine, ask_for_confirmation=True)
                            logging.info("Authentication order unset and applied successfully")

                    # STEP 2: Now unset all other AAA components (except auth order which we already unset)
                    unset_cli_cmd += " ".join([f"{unset_system_cli} {NvosConst.SYSTEM_AAA} {aaa_comp}; " for
                                               aaa_comp in aaa_config.keys() if
                                               aaa_comp != "authentication-order" and  # Skip old format (already unset)
                                               aaa_comp != NvosConst.SYSTEM_AUTHENTICATION and  # Skip new format (already unset)
                                               aaa_comp != NvosConst.SYSTEM_AAA_USER and
                                               aaa_comp != NvosConst.SYSTEM_AAA_CLASS and
                                               aaa_comp != NvosConst.SYSTEM_AAA_ROLE])

                    # unset other system components
                    unset_cli_cmd += " ".join([f"{unset_system_cli} {set_comp_name}; " for set_comp_name in
                                               system_config.keys() if set_comp_name != NvosConst.SYSTEM_AAA])

                    logging.info("Execute remaining system unset commands")
                    dut_engine.run_cmd(unset_cli_cmd)

                with allure.step("Set base configurations"):
                    set_base_configurations_cl(dut_engine=dut_engine, apply=False)

            with allure.step("Apply configurations"):
                NvueGeneralCli.apply_config(dut_engine, ask_for_confirmation=True)

            if should_wait_for_nvued_after_apply:
                DutUtilsTool.wait_for_nvos_to_become_functional(dut_engine).verify_result()
            if active_port:
                active_port.interface.wait_for_port_state(state='up', dut_engine=dut_engine).verify_result()


def clear_system_profile_config(dut_engine=None):
    with allure.step("Clear system profile"):
        system = System(None)
        system_profile_output = OutputParsingTool.parse_json_str_to_dictionary(
            system.profile.show(dut_engine=dut_engine)).get_returned_value()
        try:
            ValidationTool.validate_fields_values_in_output(SystemConsts.PROFILE_OUTPUT_FIELDS,
                                                            SystemConsts.DEFAULT_SYSTEM_PROFILE_VALUES,
                                                            system_profile_output).verify_result()
        except AssertionError:
            system.profile.action_profile_change(
                params_dict={'adaptive-routing': 'enabled', 'breakout-mode': 'disabled'}, engine=dut_engine)
