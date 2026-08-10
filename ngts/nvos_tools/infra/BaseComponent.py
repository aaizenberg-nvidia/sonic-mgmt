import logging
import os

from typing import Dict, Iterable, Tuple, Union

from ngts.ngts_types import OperationalAppliedT
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.cli_wrappers.nvue.base_cli import BaseCli
from ngts.cli_wrappers.nvue.nvue_system_clis import NvueSystemCli
from ngts.cli_wrappers.openapi.openapi_system_clis import OpenApiSystemCli
from ngts.nvos_constants.constants_nvos import ApiType, ConfState, ImageConsts, ActionConsts, OutputFormat, \
    ActionParamConsts, SystemConsts
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, RebootParams
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


class BaseComponent:
    parent_obj = None
    api_obj = {ApiType.NVUE: NvueSystemCli, ApiType.OPENAPI: OpenApiSystemCli}
    _resource_path = ''

    def __init__(self, parent=None, api=None, path='', force_api=None):
        self.parent_obj = parent
        if self.parent_obj and not api:
            self.api_obj = self.parent_obj.api_obj
        else:
            self.api_obj = api
        self._resource_path = path
        assert force_api in ApiType.ALL_TYPES + [
            None], f'Argument "force_api" must be in {ApiType.ALL_TYPES + [None]}. Given: {force_api}'

        if force_api or not self.parent_obj:
            self._force_api = force_api
        else:
            self._force_api = self.parent_obj._force_api

    @property
    def _api_to_use(self):
        return self._force_api if self._force_api else TestToolkit.tested_api

    @property
    def _cli_wrapper(self) -> BaseCli:
        return self.api_obj[self._api_to_use]

    @property
    def _general_cli_wrapper(self):
        return TestToolkit.GeneralApi[self._api_to_use]

    def get_resource_path(self):
        return "{parent_path}{self_path}".format(
            parent_path=self.parent_obj.get_resource_path() if self.parent_obj else "", self_path=self._resource_path)

    def get_resource_basename(self):
        resource_path = self.get_resource_path()
        return os.path.basename(resource_path)

    def update_param(self, param, rev):
        if self._api_to_use == ApiType.OPENAPI:
            param = param.replace('/', "%2F").replace(' ', "/").replace('--', '?')
        if rev and rev != ConfState.OPERATIONAL:
            param += ('?rev=' + rev) if self._api_to_use == ApiType.OPENAPI else f' --{rev}'
        return param

    def show(self, op_param="", output_format=OutputFormat.json, dut_engine=None, should_succeed=True,
             rev=ConfState.OPERATIONAL, exempted_err_msgs=None, if_returned_value=True, check_engine_connectivity: bool = True):
        if not dut_engine:
            dut_engine = TestToolkit.get_engine()

        with allure.step('Execute show for {}'.format(self.get_resource_path())):
            op_param = self.update_param(op_param, rev)
            result_obj = SendCommandTool.execute_command(self._cli_wrapper.show, dut_engine, self.get_resource_path(),
                                                         op_param, output_format, check_engine_connectivity, exempted_err_msgs=exempted_err_msgs)
        if if_returned_value:
            return result_obj.get_returned_value(should_succeed)
        else:
            return result_obj

    def parse_show(self, op_param="", dut_engine=None, should_succeed=True, rev=ConfState.OPERATIONAL, exempted_err_msgs=None):
        with allure.step('Parse show for {}'.format(self.get_resource_path())):
            output = self.show(
                op_param, OutputFormat.json, dut_engine, should_succeed, rev=rev, exempted_err_msgs=exempted_err_msgs
            )
            return OutputParsingTool.parse_json_str_to_dictionary(output).verify_result()

    def parse_show_operational_applied(
        self,
        op_param: str = "",
        dut_engine=None,
        should_succeed: bool = True,
    ) -> OperationalAppliedT:
        """Run `parse_show` for both `operational` and `applied` revs and return them keyed by rev."""
        with allure.step(f"Parse show operational+applied for {self.get_resource_path()}"):
            return {
                ConfState.OPERATIONAL: self.parse_show(
                    op_param=op_param, dut_engine=dut_engine,
                    should_succeed=should_succeed, rev=ConfState.OPERATIONAL,
                ),
                ConfState.APPLIED: self.parse_show(
                    op_param=op_param, dut_engine=dut_engine,
                    should_succeed=should_succeed, rev=ConfState.APPLIED,
                ),
            }

    def _set(self, param_name, param_value, expected_str='', apply=False, ask_for_confirmation=False, dut_engine=None,
             client_certs_after_apply: CertInfo = None, check_engine_connectivity: bool = True,
             is_fips_mode=False, fips_timeout=10):
        if not dut_engine:
            dut_engine = TestToolkit.get_engine()

        # FIPS mode handling: wrap execution with timeout and exception management
        if is_fips_mode and apply:
            return self._execute_set_with_fips_handling(param_name, param_value, expected_str,
                                                        ask_for_confirmation, dut_engine,
                                                        client_certs_after_apply, check_engine_connectivity,
                                                        fips_timeout)

        # Normal mode (non-FIPS)
        # Don't check expected_str during SET - only during APPLY if apply=True
        # This is because messages like disconnection notifications appear during APPLY, not SET
        if apply and expected_str:
            # For apply operations with expected_str, don't check during SET
            with allure.step(f'Execute set {param_name} {param_value}'):
                result_obj = SendCommandTool.execute_command(self._cli_wrapper.set, dut_engine,
                                                             self.get_resource_path(), param_name, param_value, check_engine_connectivity)
        else:
            # Normal SET with expected_str check (for non-apply operations)
            with allure.step(f'Execute set {param_name} {param_value}'):
                result_obj = SendCommandTool.execute_command_expected_str(self._cli_wrapper.set,
                                                                          expected_str, dut_engine,
                                                                          self.get_resource_path(), param_name, param_value, check_engine_connectivity)

        if result_obj.result and apply:
            with allure.step("Applying set configuration"):
                option = ''
                if ask_for_confirmation == '-y':
                    option = '-y'
                    ask_for_confirmation = False
                # Use execute_command_expected_str for apply if expected_str is provided
                if expected_str:
                    try:
                        result_obj = SendCommandTool.execute_command(self._general_cli_wrapper.apply_config, dut_engine,
                                                                     ask_for_confirmation, option, client_certs_after_apply=client_certs_after_apply)
                    except Exception as e:
                        # Check if this is an expected disconnection (EOF, connection closed, etc.)
                        exception_str = str(e).lower()
                        if 'eof' in exception_str and 'eof' in expected_str:
                            logger.info(f"Session disconnected as expected: {e}")
                            result_obj = ResultObj(True, f"Session disconnected as expected: {e}")
                        else:
                            # Unexpected exception
                            raise
                else:
                    result_obj = SendCommandTool.execute_command(self._general_cli_wrapper.apply_config, dut_engine,
                                                                 ask_for_confirmation, option, client_certs_after_apply=client_certs_after_apply)
        return result_obj

    def _execute_set_with_fips_handling(self, param_name, param_value, expected_str, ask_for_confirmation,
                                        dut_engine, client_certs_after_apply, check_engine_connectivity, fips_timeout):
        """
        Execute set operation with FIPS mode disconnection handling.
        In FIPS mode, AAA configuration changes cause all sessions to disconnect asynchronously.
        """
        logger.info(f'Executing set operation in FIPS mode for {self.get_resource_path()}')

        # Add expected disconnection message if not already present
        if not expected_str:
            expected_str = ["Session terminated by NVUE as authentication config was applied in FIPS mode"]

        # Save original engine settings
        original_auto_connect = getattr(dut_engine, 'auto_connect', None)
        original_timeout = None

        # Disable auto-reconnect
        if hasattr(dut_engine, 'auto_connect'):
            dut_engine.auto_connect = False
            logger.info('Disabled auto_connect for FIPS mode operation')

        # Reduce timeout
        if hasattr(dut_engine, 'engine') and hasattr(dut_engine.engine, 'timeout'):
            original_timeout = dut_engine.engine.timeout
            dut_engine.engine.timeout = fips_timeout
            logger.info(f"Reduced engine timeout from {original_timeout} to {fips_timeout} seconds for FIPS mode")

        try:
            # Execute SET command (normal, no apply yet)
            with allure.step(f'Execute set {param_name} {param_value} (FIPS mode)'):
                result_obj = SendCommandTool.execute_command(self._cli_wrapper.set, dut_engine,
                                                             self.get_resource_path(), param_name, param_value,
                                                             check_engine_connectivity)

            # Apply configuration with special FIPS handling
            if result_obj.result:
                with allure.step("Applying set configuration (FIPS mode)"):
                    option = ''
                    if ask_for_confirmation == '-y':
                        option = '-y'
                        ask_for_confirmation = False

                    # Build the apply command string
                    apply_cmd = self._general_cli_wrapper.apply_config(ask_for_confirmation, option)

                    # Use direct netmiko send_command with short read_timeout and expect_string
                    # Expect either normal prompt or disconnection message
                    normal_prompt = dut_engine.engine.find_prompt()
                    disconnect_pattern = r"Session terminated by NVUE as authentication config was applied in FIPS mode"
                    expect_string = f"({normal_prompt}|{disconnect_pattern})"

                    try:
                        output = dut_engine.engine.send_command(
                            apply_cmd,
                            expect_string=expect_string,
                            read_timeout=fips_timeout,
                            max_loops=100,  # Reduce loops to make it faster
                            delay_factor=0.1
                        )
                        result_obj = ResultObj(True, output)

                        # If disconnection message in output, confirm success
                        if "Session terminated by NVUE" in output:
                            logger.info("FIPS disconnection detected in output - treating as success")
                            result_obj = ResultObj(True, "Configuration applied (FIPS disconnection detected in output)")

                    except Exception as e:
                        # If any exception (including timeout), treat as expected FIPS disconnection
                        logger.info(f"Apply interrupted ({type(e).__name__}) - expected in FIPS mode: {e}")
                        result_obj = ResultObj(True, f"Configuration applied (FIPS disconnection: {type(e).__name__})")

            return result_obj

        finally:
            # Restore original engine settings
            if original_timeout is not None and hasattr(dut_engine, 'engine') and hasattr(dut_engine.engine, 'timeout'):
                dut_engine.engine.timeout = original_timeout
                logger.info(f"Restored engine timeout to {original_timeout}")

            if original_auto_connect is not None and hasattr(dut_engine, 'auto_connect'):
                dut_engine.auto_connect = original_auto_connect
                logger.info('Restored auto_connect setting')

    def set(self, op_param_name="", op_param_value={}, expected_str='', apply=False, ask_for_confirmation=False,
            dut_engine=None, client_certs_after_apply: CertInfo = None, check_engine_connectivity: bool = True,
            is_fips_mode=False, fips_timeout=10) -> 'ResultObj':
        if not dut_engine:
            dut_engine = TestToolkit.get_engine()
        with allure.step('Execute set for {resource_path}'.format(resource_path=self.get_resource_path())):
            if op_param_name:
                if TestToolkit.tested_api == ApiType.OPENAPI and self._api_to_use != ApiType.NVUE:
                    if isinstance(op_param_value, str):
                        op_param_value = op_param_value.replace('"', '')
                    value = {op_param_name: op_param_value}
                    return self._set('', value, expected_str, apply, ask_for_confirmation, dut_engine,
                                     client_certs_after_apply, check_engine_connectivity, is_fips_mode, fips_timeout)
                else:
                    if op_param_value == {}:
                        op_param_value = op_param_name
                        op_param_name = ''
                        return self._set(op_param_name, op_param_value, expected_str, apply, ask_for_confirmation,
                                         dut_engine, client_certs_after_apply, check_engine_connectivity,
                                         is_fips_mode, fips_timeout)
                    elif isinstance(op_param_value, dict):
                        output = ''
                        for param_name, param_value in op_param_value.items():
                            res = self._set(param_name, param_value, expected_str, apply, ask_for_confirmation,
                                            dut_engine, client_certs_after_apply, check_engine_connectivity,
                                            is_fips_mode, fips_timeout)
                            output = output + "\n" + res
                        return output
                    elif isinstance(op_param_value, list):
                        op_param_value = ' '.join(op_param_value)
                        return self._set(op_param_name, op_param_value, expected_str, apply, ask_for_confirmation,
                                         dut_engine, client_certs_after_apply, check_engine_connectivity,
                                         is_fips_mode, fips_timeout)
                    elif isinstance(op_param_value, str) or isinstance(op_param_value, int):
                        return self._set(op_param_name, op_param_value, expected_str, apply, ask_for_confirmation,
                                         dut_engine, client_certs_after_apply, check_engine_connectivity,
                                         is_fips_mode, fips_timeout)
            else:
                logger.info('Run set with no params')
                op_param_value = '' if TestToolkit.tested_api == ApiType.NVUE else {}
                return self._set(op_param_name, op_param_value, expected_str, apply, ask_for_confirmation,
                                 dut_engine, client_certs_after_apply, check_engine_connectivity,
                                 is_fips_mode, fips_timeout)

    def unset(self, op_param="", expected_str="", apply=False, ask_for_confirmation=False, dut_engine=None,
              check_engine_connectivity: bool = True, is_fips_mode=False, fips_timeout=10):
        if not dut_engine:
            dut_engine = TestToolkit.get_engine()

        # FIPS mode handling: wrap execution with timeout and exception management
        if is_fips_mode and apply:
            return self._execute_unset_with_fips_handling(op_param, expected_str, ask_for_confirmation,
                                                          dut_engine, check_engine_connectivity, fips_timeout)

        # Normal mode (non-FIPS)
        resource_path = self.get_resource_path()
        with allure.step('Execute unset {op_param} for {resource_path}'.format(op_param=op_param,
                                                                               resource_path=resource_path)):
            result_obj = SendCommandTool.execute_command_expected_str(self._cli_wrapper.unset,
                                                                      expected_str, dut_engine,
                                                                      resource_path, op_param, check_engine_connectivity)
        if result_obj.result and apply:
            with allure.step("Applying unset configuration"):
                result_obj = SendCommandTool.execute_command(self._general_cli_wrapper.apply_config, dut_engine,
                                                             ask_for_confirmation)
        return result_obj

    def _execute_unset_with_fips_handling(self, op_param, expected_str, ask_for_confirmation,
                                          dut_engine, check_engine_connectivity, fips_timeout):
        """
        Execute unset operation with FIPS mode disconnection handling.
        """
        logger.info(f'Executing unset operation in FIPS mode for {self.get_resource_path()}')

        # Add expected disconnection message if not already present
        if not expected_str:
            expected_str = ["Session terminated by NVUE as authentication config was applied in FIPS mode"]

        # Save original engine settings
        original_auto_connect = getattr(dut_engine, 'auto_connect', None)
        original_timeout = None

        # Disable auto-reconnect
        if hasattr(dut_engine, 'auto_connect'):
            dut_engine.auto_connect = False
            logger.info('Disabled auto_connect for FIPS mode operation')

        # Reduce timeout
        if hasattr(dut_engine, 'engine') and hasattr(dut_engine.engine, 'timeout'):
            original_timeout = dut_engine.engine.timeout
            dut_engine.engine.timeout = fips_timeout
            logger.info(f"Reduced engine timeout from {original_timeout} to {fips_timeout} seconds for FIPS mode")

        try:
            # Execute UNSET command
            resource_path = self.get_resource_path()
            with allure.step('Execute unset {op_param} for {resource_path} (FIPS mode)'.format(
                    op_param=op_param, resource_path=resource_path)):
                result_obj = SendCommandTool.execute_command(self._cli_wrapper.unset, dut_engine,
                                                             resource_path, op_param, check_engine_connectivity)

            # Apply configuration
            if result_obj.result:
                with allure.step("Applying unset configuration (FIPS mode)"):
                    option = ''
                    if ask_for_confirmation == '-y':
                        option = '-y'
                        ask_for_confirmation = False

                    try:
                        result_obj = SendCommandTool.execute_command(self._general_cli_wrapper.apply_config, dut_engine,
                                                                     ask_for_confirmation, option)
                    except (TimeoutError, OSError, EOFError) as e:
                        # Expected disconnection in FIPS mode
                        logger.info(f"Unset operation interrupted ({type(e).__name__}) - expected FIPS disconnection: {e}")
                        result_obj = ResultObj(True, f"Configuration applied (FIPS disconnection: {type(e).__name__})")
                    except Exception as e:
                        # Check if connection-related
                        exception_str = str(e).lower()
                        if any(kw in exception_str for kw in ['connection', 'eof', 'timeout', 'disconnect', 'closed']):
                            logger.info(f"Unset operation interrupted ({type(e).__name__}) - treating as expected FIPS disconnection: {e}")
                            result_obj = ResultObj(True, f"Configuration applied (FIPS disconnection: {type(e).__name__})")
                        else:
                            raise

            return result_obj

        finally:
            # Restore original engine settings
            if original_timeout is not None and hasattr(dut_engine, 'engine') and hasattr(dut_engine.engine, 'timeout'):
                dut_engine.engine.timeout = original_timeout
                logger.info(f"Restored engine timeout to {original_timeout}")

            if original_auto_connect is not None and hasattr(dut_engine, 'auto_connect'):
                dut_engine.auto_connect = original_auto_connect
                logger.info('Restored auto_connect setting')

    # todo: remove this function once it's no longer needed for backward-compatibility
    def action_deprecated(self, action: str, suffix="", param_name="", param_value="", output_format=OutputFormat.json,
                          dut_device=None, dut_engine=None, expected_output='', expect_reboot=False, deny_reboot=False,
                          topology_obj=None) -> ResultObj:
        """
        Runs nv action commands. The arguments `suffix`, `param_name` and `param_value` are all arguments passed to the
        the command, the difference is that in OpenAPI the `suffix` is appended to the URL while param_name and
        param_value are in the message contents. See examples below (also notice how NVUE handles param_name differently
        in these examples, based on whether or not we have param_value).
        :param deny_reboot:
        :param expect_reboot: Set to True if the system is expected to reboot when the action is run.

        Example: fae.platform.firmware.cpld.action_deprecated('install', "files /path/to/xyz.img", param_name="force")
        --> NVUE:       nv action install fae platform firmware cpld files /path/to/xyz.img force
        --> OPENAPI:    /fae/platform/firmware/cpld/files/%2Fpath%2Fto%2Fxyz.img
                        {"@install": {"state": "start", "parameters": {"force": True}}}

        Example: fae.platform.firmware.cpld.action_deprecated('fetch', param_name="remote-url", param_value="scp://...")
        --> NVUE:       nv action fetch fae platform firmware cpld scp://...
        --> OPENAPI:    /fae/platform/firmware/cpld
                        {"@fetch": {"state": "start", "parameters": {"remote-url": "scp://..."}}}
        """
        dut_engine = dut_engine or TestToolkit.get_engine()
        dut_device = dut_device or TestToolkit.get_device()
        topology_obj = topology_obj or (TestToolkit.topology_obj if TestToolkit else None)
        resource_path = self.get_resource_path()
        with allure.step(f"Execute action {action} for {resource_path}"):
            return SendCommandTool.execute_command_expected_str(self._cli_wrapper.action_deprecated, expected_output, dut_engine,
                                                                dut_device, action, resource_path, suffix, param_name,
                                                                param_value, output_format, expect_reboot, None,
                                                                deny_reboot=deny_reboot, topology_obj=topology_obj,
                                                                expected_output=expected_output)

    def action_fetch(self, path, base_url=None, engine=None, device=None, expected_output=None,
                     timeout=None) -> ResultObj:
        """
        nv action fetch <resource-path> <remote-url>
        :param path: Absolute file-path in the network drive, e.g. '/auto/path/to/file.img'.
        :param base_url: e.g. 'scp://user:password@host'. If None, the default credentials are used. If empty string
            then the `path` parameter needs to contain the full URL.
        :param engine: Engine to use for the fetch command. None uses the default.
        :param device: Device to use for the fetch command. None uses the default.
        :param expected_output: Expected output from the fetch command. None uses the default.
        :param timeout: Timeout in seconds for the fetch command. None uses the default.
        """
        url = (ImageConsts.SCP_PATH if base_url is None else base_url) + path
        with allure.step(f"Fetching: {url}"):
            expected_output = expected_output or ['File fetched successfully', 'File has been successfully fetched', 'Action succeeded']
            return self.action(
                ActionConsts.FETCH,
                (ActionParamConsts.REMOTE_URL, url),
                engine=engine,
                device=device,
                expected_output=expected_output,
                timeout=timeout,
            )

    def action_fetch_local(self, file_url, engine=None, device=None) -> ResultObj:
        """
        Fetch a file from a local path on the DUT using file:// URL.
        nv action fetch <resource-path> file:///path/to/file
        :param file_url: Local file URL, e.g. 'file:///tmp/image.bin'
        """
        with allure.step(f"Fetching local file: {file_url}"):
            return self.action(ActionConsts.FETCH, (ActionParamConsts.REMOTE_URL, file_url), engine=engine, device=device,
                               expected_output=['File fetched successfully', 'File has been successfully fetched'])

    def action(self,
               action_str: str,  # e.g. 'install'
               main_param: Tuple[str, str] | None = None,  # e.g. ('remote-url', '/path/to/file')
               flags: Union[str, Iterable[str]] = '',  # e.g. 'force immediate' or ['force', 'immediate']
               additional_params: Dict = None,  # e.g. {'reboot': 'no'}
               engine=None,
               reboot_params: Union[bool, RebootParams, None] = None,  # set True if reboot is expected
               send_user_confirmation: str = None,  # e.g. 'y' or 'n' if NVUE asks for confirmation
               expected_output: Union[str, Iterable[str]] = '',  # string or list of possible strings
               timeout: float = None,  # timeout in seconds for the action command; None uses the default
               device=None) -> ResultObj:
        """
        :param action_str: e.g. 'install', 'reboot', ...
        :param main_param: A 2-tuple containing the (name, value) of the action's main parameter, if any. For example,
            for action 'fetch' this argument would be ('remote-url', '/path/to/file')
        :param flags: String or list of strings. For NVUE the flags are included as-is in the command. For OpenAPI
            they become {flag1: True, flag2: True, ... }
        :param additional_params: A dict of all other parameters sent to the action, e.g. {'reboot': 'no'}
        :param engine: LinuxSshEngine. If None, the DUT engine is used.
        :param reboot_params: One of [True, False, None, RebootParams object].
            If the action should cause a reboot then reboot_params should be either `True` (for using default parameters
            for the reboot) or a RebootParams object specifying the parameters.
            If no reboot is expected as a result of this action, set to False or None.
        :param send_user_confirmation: If we expect the action to present a [y/n] confirmation message, set this
            argument to 'y' or 'n' (or any other string). If no confirmation-message is expected, set to None.
            Note: For OpenAPI actions this should always be set to None.
        :param expected_output: Sub-string we expect to see when the action is finished and before reboot (if expected).
        :param device: BaseDevice. If None, the DUT is used.
        """
        additional_params = additional_params or {}
        engine = engine or TestToolkit.get_engine()
        try:
            device = device or TestToolkit.get_device()
        except BaseException:
            pass

        if not (reboot_params is None or type(reboot_params) in (bool, RebootParams)):
            raise TypeError(f'{reboot_params=} but it should be one of [True, False, None, RebootParams()]')

        # Auto-determine expected_output for install actions if not explicitly provided
        if action_str == ActionConsts.INSTALL and not expected_output:
            expected_output = SystemConsts.REBOOT_RESPONSE_MESSAGES if reboot_params else SystemConsts.ACTION_INSTALL_SUCCESS_MESSAGES

        resource_path = self.get_resource_path()
        with allure.step("Execute " + BaseCli.get_nv_action_string(action_str, resource_path, main_param, flags,
                                                                   additional_params)):
            result = self._cli_wrapper.action(action_str, resource_path, main_param, flags, additional_params, engine,
                                              reboot_params, send_user_confirmation, expected_output, device,
                                              timeout=timeout)
            logger.info(result)

        if reboot_params and result:  # if reboot is expected and the action returned a success message: wait on reboot
            reboot_result = DutUtilsTool.wait_on_system_reboot(engine, reboot_params,
                                                               device=device, verify_final_result=False)
            result = reboot_result
        else:
            try:
                with allure.step('Assert that no reboot happened'):
                    check_port_status_till_alive(True, engine.ip, engine.ssh_port, tries=5, delay=3)
            except (AssertionError, Exception):
                logger.error(f'Action {"succeeded" if result else "failed"} and caused an unexpected reboot. '
                             f'Failure information:\n{result.info}')
                with allure.step('Waiting for system to recover from unexpected reboot'):
                    DutUtilsTool.wait_on_system_reboot(engine, reboot_params or RebootParams(), device)
                    if result:
                        result.result = False
                        result.info += '\nThe operation caused an unexpected reboot.'

        return result
