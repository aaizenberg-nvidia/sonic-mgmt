import logging
import time

import requests

from ngts.nvos_constants.constants_nvos import OpenApiReqType, SystemConsts, ActionConsts
from ngts.nvos_constants.constants_nvos import OutputFormat
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime, SubDuration
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, RebootParams
from .openapi_command_builder import OpenApiCommandHelper
from ..nvue.nvue_base_clis import BaseCli
from ...nvos_tools.infra import ExceptionTool
from ...nvos_tools.infra.ResultObj import ResultObj, IssueType
from ...nvos_tools.infra.SendCommandTool import SendCommandTool
from ...nvos_tools.infra.ValidationTool import ValidationTool

logger = logging.getLogger()


class OpenApiBaseCli(BaseCli):
    cli_name = ""

    @classmethod
    def action(cls, action_str, resource_path, main_param, flags, additional_params, engine, reboot_params,
               send_user_confirmation, expected_output, device, timeout=None):
        """See documentation of BaseComponent.action()"""
        # Note: read_timeout is not used for OpenAPI actions (HTTP requests have their own timeout handling)
        if send_user_confirmation:
            logger.warning(f'The following argument is ignored for OpenAPI commands: {send_user_confirmation=}')
        if timeout is not None:
            logger.warning(f'The following argument is ignored for OpenAPI commands: {timeout=}. '
                           f'OpenAPI actions use REST polling with no overall timeout, unlike NVUE which uses '
                           f'Netmiko send_command_timing with a read_timeout.')

        url = cls._resource_path_to_rest_path(resource_path)
        params = additional_params.copy()
        if main_param:  # in OpenAPI the main_param is just like any other parameter so we add it to the params dict
            params.update((main_param, ))  # main_param=(a, b) is converted to a dict item --> {a: b}
        if isinstance(flags, str):
            flags = flags.split()
        params.update({flag: True for flag in flags})
        data = {'state': 'start', 'parameters': params}

        logger.info(f'Sending "{action_str}" to {resource_path} with {params=}')

        try:
            output = OpenApiCommandHelper.execute_action(OpenApiBaseCli._action_key(action_str), engine.engine.username,
                                                         engine.engine.password, engine.ip, engine.open_api_port, url,
                                                         data, expected_output)
        except requests.exceptions.RequestException as e:
            result = ResultObj(False,
                               info=(f'Possible connection loss: {ExceptionTool.format_exception(e)}.\n'
                                     f'This is probably due to a reboot.'))
            result.update_traceback()
        else:
            if output.startswith(OpenApiCommandHelper.ACTION_ERROR):
                # Special handling for "Close client sessions failed" error during reboot
                # This specific error is expected and should not fail the reboot action
                error_message = output.partition(' ')[2]
                is_expected_reboot_error = (
                    action_str == ActionConsts.REBOOT and
                    'Close client sessions failed' in error_message
                )
                if not is_expected_reboot_error:
                    return ResultObj(False, output, error_message)

                logger.info(f"Reboot action: Ignoring expected error '{error_message}'")
                output = error_message

            result = ValidationTool.verify_any_string_in_string(output, expected_output)
        return result

    @staticmethod
    def show(engine, resource_path, op_param="", output_format=OutputFormat.json, check_engine_connectivity: bool = True):
        logging.info("Running GET method on dut using openApi for {}".format(resource_path))
        dut_engine = engine.engine if check_engine_connectivity else engine

        return OpenApiCommandHelper.execute_script(dut_engine.username, dut_engine.password, OpenApiReqType.GET,
                                                   engine.ip, engine.open_api_port, resource_path, op_param)

    @staticmethod
    def filter(engine, resource_path, filter_name="", value="", check_engine_connectivity: bool = True):
        logging.info("Running GET method (filter) on dut using openApi for {}".format(resource_path))
        dut_engine = engine.engine if check_engine_connectivity else engine
        params = f'?filter={filter_name}%3d{value}' if filter_name else ''
        return OpenApiCommandHelper.execute_script(dut_engine.username, dut_engine.password, OpenApiReqType.GET,
                                                   engine.ip, engine.open_api_port, resource_path, params)

    @staticmethod
    def set(engine, resource_path, op_param_name="", op_param_value="", check_engine_connectivity: bool = True):
        logging.info("Running PATCH method on dut using openApi for {}".format(resource_path))
        dut_engine = engine.engine if check_engine_connectivity else engine

        return OpenApiCommandHelper.execute_script(dut_engine.username, dut_engine.password, OpenApiReqType.PATCH,
                                                   engine.ip, engine.open_api_port, resource_path, op_param_name,
                                                   op_param_value)

    @staticmethod
    def unset(engine, resource_path, op_param="", check_engine_connectivity: bool = True):
        logging.info("Running DELETE method on dut using openApi for {}".format(resource_path))
        dut_engine = engine.engine if check_engine_connectivity else engine

        return OpenApiCommandHelper.execute_script(dut_engine.username, dut_engine.password, OpenApiReqType.DELETE,
                                                   engine.ip, engine.open_api_port, resource_path, op_param, None)

    @staticmethod
    def _resource_path_to_rest_path(resource_path: str, suffix=''):
        output = resource_path.replace(' ', '/')
        if suffix:
            output += '/' + suffix.replace('/', '%2F').replace(' ', '/')
        return output

    @staticmethod
    def _action_key(action: str):
        return '@' + action

    @staticmethod
    def action_deprecated(engine, device=None, action_type='', resource_path='', suffix="", param_name="", param_value="",
                          output_format=None, expect_reboot=False, recovery_engine=None, topology_obj=None, should_succeed=True,
                          system_is_ready_timeout=None, track_boot_intervals=False, deny_reboot=False, press_y=False,
                          expected_output=''):
        """See documentation of BaseComponent.action_deprecated"""
        if not action_type:
            raise ValueError("action_type must be non-empty")
        if not resource_path:
            raise ValueError("resource_path must be non-empty")

        url = OpenApiBaseCli._resource_path_to_rest_path(resource_path, suffix)
        data = {'state': 'start'}
        if isinstance(param_name, list):
            data['parameters'] = {}
            for param, value in zip(param_name, param_value):
                data['parameters'][param] = True if (value == '') else value
        else:
            if param_name:
                data['parameters'] = {param_name: (True if (param_value == '') else param_value)}
        if not expected_output and (action_type == 'reboot' or expect_reboot):
            # Temporary workaround before refactoring action()
            expected_output = SystemConsts.REBOOT_RESPONSE_MESSAGES

        op_start = time.perf_counter()
        result = OpenApiCommandHelper.execute_action(OpenApiBaseCli._action_key(action_type), engine.engine.username,
                                                     engine.engine.password, engine.ip, engine.open_api_port, url, data,
                                                     expected_output)
        op_duration = time.perf_counter() - op_start
        OperationTime.record_sub_duration(SubDuration.OPERATION, op_duration)

        if deny_reboot:
            return result

        elif ((expect_reboot or any(msg in result for msg in SystemConsts.REBOOT_RESPONSE_MESSAGES)) and
                "abort" not in result):

            reboot_start = time.perf_counter()
            DutUtilsTool.wait_on_system_reboot(
                engine,
                reboot_params=RebootParams(recovery_engine=recovery_engine, topology_obj=topology_obj,
                                           system_is_ready_timeout=system_is_ready_timeout,
                                           track_boot_intervals=track_boot_intervals),
                device=device)
            OperationTime.record_sub_duration(SubDuration.REBOOT, time.perf_counter() - reboot_start)

        return result
