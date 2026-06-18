import logging
import os
import time
from typing import Tuple, Union

import pytest
from retry import retry
from ngts.cli_wrappers.nvue.nvue_system_clis import NvueSystemCli
from ngts.cli_wrappers.openapi.openapi_system_clis import OpenApiSystemCli
from ngts.constants.constants import InfraConst
from ngts.helpers.sanitizer_helper import check_sanitizer_and_store_dump
from ngts.nvos_constants.constants_nvos import ApiType, SystemConsts, HealthConsts, ActionConsts
from ngts.nvos_constants.constants_nvos import OutputFormat
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.system.Aaa import Aaa
from ngts.nvos_tools.system.Config import Config
from ngts.nvos_tools.system.ControlPlane import ControlPlane
from ngts.nvos_tools.system.Debug_log import DebugLog
from ngts.nvos_tools.system.Disk import Disk
from ngts.nvos_tools.system.Events import Events
from ngts.nvos_tools.system.Files import Files
from ngts.nvos_tools.system.GnmiServer import GnmiServer
from ngts.nvos_tools.system.Health import Health
from ngts.nvos_tools.system.Image import Image
from ngts.nvos_tools.system.Lldp import Lldp
from ngts.nvos_tools.system.Log import Log
from ngts.nvos_tools.system.MTLSableServerResource import MTLSableServerResource
from ngts.nvos_tools.system.Ntp import Ntp
from ngts.nvos_tools.system.Profile import Profile
from ngts.nvos_tools.system.Packages import Packages
from ngts.nvos_tools.system.Reboot import Reboot
from ngts.nvos_tools.system.Security import Security
from ngts.nvos_tools.system.SshServer import SshServer
from ngts.nvos_tools.system.SnmpServer import SnmpServer
from ngts.nvos_tools.system.Stats import Stats
from ngts.nvos_tools.system.Syslog import Syslog
from ngts.nvos_tools.system.Techsupport import TechSupport
from ngts.nvos_tools.system.Telemetry import Telemetry
from ngts.nvos_tools.system.Version import Version
from ngts.nvos_tools.system.Ztp import Ztp
from ngts.nvos_tools.system.Dns import Dns
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.GrpcTunnel import GrpcTunnel

logger = logging.getLogger()


class System(BaseComponent):
    def __init__(self, parent_obj=None, devices_dut=None, force_api=None):
        assert force_api in ApiType.ALL_TYPES + [None], f'Argument "force_api" must be in {ApiType.ALL_TYPES + [None]}. Given: {force_api}'
        BaseComponent.__init__(self, parent=parent_obj,
                               api={ApiType.NVUE: NvueSystemCli, ApiType.OPENAPI: OpenApiSystemCli}, path='/system', force_api=force_api)
        self.config = Config(self)
        self.documentation = Documentation(self)
        self.aaa = Aaa(self)
        self.log = Log(self)
        self.debug_log = DebugLog(self)
        self.snmp_server = SnmpServer(self)
        self.security = Security(self)
        self.ssh_server = SshServer(self)
        self.system_cli = BaseComponent(self, path='/cli')
        self.serial_console = BaseComponent(self, path='/serial-console')
        self.syslog = Syslog(self)
        self.ntp = Ntp(self)
        self.telemetry = Telemetry(self)
        self.ztp = Ztp(self)
        self.stats = Stats(self, devices_dut)
        self.techsupport = TechSupport(self)
        self.image = Image(self)
        self.message = BaseComponent(self, path='/message')
        self.version = Version(self)
        self.events = Events(self)
        self.dns = Dns(self)
        self.reboot = Reboot(self)
        self.factory_default = FactoryDefault(self)
        self.profile = Profile(self)
        self.health = Health(self)
        self.datetime = DateTime(self)
        self.gnmi_server = GnmiServer(self)
        self.grpc_tunnel = GrpcTunnel(self)
        self.web_server_api = WebServerAPI(self)
        self.api = Api(self)
        self.ptp = BaseComponent(self, path='/ptp')
        self.plane_port = BaseComponent(self, path='/plane-port')
        self.lldp = Lldp(self)
        self.disk = Disk(self)
        self.memory = BaseComponent(self, path='/memory')
        self.cpu = BaseComponent(self, path='/cpu')
        self.asic_debug_config = BaseComponent(self, path='/asic-debug-config')
        self.cpu_debug_config = BaseComponent(self, path='/cpu-debug-config')
        self.bmc_debug_config = BaseComponent(self, path='/bmc-debug-config')
        self.nv_bridge = BaseComponent(self, path='/nv-bridge')
        self.control_plane = ControlPlane(self)
        self.packages = Packages(self)

    @staticmethod
    def get_expected_fields(device, resource):
        return device.constants.system[resource]

    def validate_health_status(self, expected_status, throw_exception=True, dut_engine=None, expected_led=None):
        if not dut_engine:
            dut_engine = TestToolkit.get_engine()
        with allure.step("Validate health status with \"nv show system\" cmd"):
            logger.info("Validate health status with \"nv show system\" cmd")
            actual_status = None
            # Retry up to 3 times if health status is N/A (may take a moment to populate after reboot)
            for attempt in range(3):
                system_output = OutputParsingTool.parse_json_str_to_dictionary(self.show(dut_engine=dut_engine)).get_returned_value()
                actual_status = system_output[SystemConsts.HEALTH][HealthConsts.STATUS]
                if actual_status == expected_status:
                    logger.info(f"Health status matches expected: {expected_status}")
                    if expected_led is not None:
                        self._validate_status_led(expected_led, throw_exception=throw_exception, dut_engine=dut_engine)
                    return
                if actual_status != "N/A":
                    break
                logger.info(f"Health status is N/A (attempt {attempt + 1}/3), sleeping 5 seconds before retry...")
                time.sleep(5)

            health_output = OutputParsingTool.parse_json_str_to_dictionary(self.health.show(dut_engine=dut_engine)).get_returned_value()
            health_issues = health_output[HealthConsts.ISSUES]

            # WA for Redmine #4963780: filter out known missing fan issues per device
            from ngts.nvos_tools.infra.RegressionConfigurations import Configurations
            engine_ip = getattr(dut_engine, 'ip', None)
            ignored_fan_prefixes = Configurations.devices_with_missing_fans.get(engine_ip, set())
            if ignored_fan_prefixes:
                health_issues = {k: v for k, v in health_issues.items()
                                 if not any(k.startswith(prefix) for prefix in ignored_fan_prefixes)}
                if not health_issues:
                    logger.info(f"All health issues are known missing fans on {engine_ip} (Redmine #4963780) - passing health check")
                    return

            health_issues_str = '\n'.join(f'{k}: {v}' for k, v in health_issues.items())
            exception_str = "Unexpected health status.\nExpected: {}, but got :{}," \
                " with the following health issues:\n{}".\
                format(expected_status, health_output[HealthConsts.STATUS], health_issues_str)
            logger.warning(exception_str)
            if throw_exception:
                assert False, exception_str

    def _validate_status_led(self, expected_led, throw_exception=True, dut_engine=None):
        # status-led is exposed by `nv show system health`, not by `nv show system`
        with allure.step(f"Validate health status-led is {expected_led!r}"):
            health_output = OutputParsingTool.parse_json_str_to_dictionary(
                self.health.show(dut_engine=dut_engine)).get_returned_value()
            actual_led = health_output.get(HealthConsts.STATUS_LED)
            if actual_led == expected_led:
                logger.info(f"Health status-led matches expected: {expected_led}")
                return
            exception_str = (
                f"Unexpected health status-led. Expected: {expected_led!r}, "
                f"but got: {actual_led!r}"
            )
            logger.warning(exception_str)
            if throw_exception:
                assert False, exception_str

    @retry(Exception, tries=10, delay=2)
    def wait_until_health_status_change_to(self, expected_status):
        self.validate_health_status(expected_status)

    def action_reboot(self, flags: Union[str, Tuple[str]] = (), send_user_confirmation=None, reboot_params=True,
                      engine=None, device=None, additional_params=None, expected_output=SystemConsts.REBOOT_RESPONSE_MESSAGES):
        """
        See documentation of BaseComponent.action().
        Examples for `flags`: 'force' ; 'force immediate' ; ['force', 'immediate'] ; ''
        """
        with allure.step('Execute action for {resource_path}'.format(resource_path=self.get_resource_path())):
            engine = engine or TestToolkit.get_engine()
            device = device or TestToolkit.get_device()

            start_time = time.time()
            result = self.action(ActionConsts.REBOOT, flags=flags, engine=engine, device=device,
                                 send_user_confirmation=send_user_confirmation, reboot_params=reboot_params, additional_params=additional_params,
                                 expected_output=expected_output)
            end_time = time.time()
            duration = end_time - start_time
            logger.info(f"Reboot and system is ready takes {duration} seconds")
            if pytest.is_sanitizer:
                dumps_folder = os.environ.get(InfraConst.ENV_LOG_FOLDER)
                with allure.step(f'check_sanitizer_and_store_dump in {dumps_folder}'):
                    check_sanitizer_and_store_dump(engine, dumps_folder, pytest.test_name)
            return result


class Documentation(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/documentation')
        self.files = Files(self)

    def action_upload(self, upload_path, file_name):
        with allure.step("Upload {file} to '{path}".format(file=file_name, path=upload_path)):
            logging.info("Upload {file} to '{path}".format(file=file_name, path=upload_path))
            return SendCommandTool.execute_command(self.api_obj[TestToolkit.tested_api].action_upload,
                                                   TestToolkit.get_engine(), self.get_resource_path(),
                                                   'files ' + file_name, upload_path)


class FactoryDefault(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/factory-default')

    def show(self, op_param="", output_format=OutputFormat.json):
        raise Exception("unset is not implemented for system/factory-default")

    def set(self, op_param_name="", op_param_value=None):
        raise Exception("unset is not implemented for system/factory-default")

    def unset(self, op_param=""):
        raise Exception("unset is not implemented for system/factory-default")

    def action_reset(self, engine=None, device=None, operation='reset factory', param="", topology_obj=None,
                     system_is_ready_timeout=None, verify_duration=False, test_name='', handle_log_analyzer=None,
                     wait_for_functional=True):
        """
        Calls factory-reset action.
        If handle_log_analyzer is True, once the action completes, the log-analyzer start-marker will be injected as the
        first log-line. If False, this will not be done. If set to None (the default) then it will be done, unless
        factory-reset is run with 'keep only-files' flag (in which case the log-files are not deleted).
        If wait_for_functional is False, the method returns as soon as the switch is reachable (port up)
        without SSH-ing in.
        """
        with allure.step("Execute factory reset {}".format(param)):
            logging.info("Execute factory reset {}".format(param))
            if not engine:
                engine = TestToolkit.get_engine()
            if not device:
                device = TestToolkit.get_device()
            from ngts.tests_nvos.system.factory_reset.helpers import KEEP_ONLY_FILES
            # can't import at top of file due to circular import
            if wait_for_functional and (handle_log_analyzer or (handle_log_analyzer is None and param != KEEP_ONLY_FILES)):
                log_analyzer_marker = TestToolkit.get_loganalyzer_marker(engine, get_full_line=True, test_string=test_name)
            else:
                log_analyzer_marker = ""
            res_obj, duration = OperationTime.save_duration(f'reset factory {param}', "", test_name, SendCommandTool.execute_command,
                                                            self.api_obj[TestToolkit.tested_api].action_reset, engine=engine, device=device, comp="factory-default", param=param, topology_obj=topology_obj,
                                                            system_is_ready_timeout=system_is_ready_timeout, check_system_is_functional=False)
            engine.disconnect()
            if wait_for_functional:
                with allure.step('wait for os to be functional'):
                    if device:
                        result_obj = device.wait_for_os_to_become_functional(engine)
                    else:
                        result_obj = DutUtilsTool.wait_for_nvos_to_become_functional(engine)
                if device:
                    device.post_reload_actions(engine)
                if log_analyzer_marker:
                    TestToolkit.add_loganalyzer_marker_at_beginning(engine, log_analyzer_marker)
            logger.info("Reset factory till system is ready takes: {} seconds".format(duration))
            res_obj.duration = duration
            return res_obj


class DateTime(BaseComponent):
    """
    @summary:
    Infra class for system.date-time field object
    """

    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/date-time')

    def action_change(self, params=""):
        rsrc_path = self.get_resource_path()
        with allure.step('Execute action change for {rsrcp} \tparams: {prm}'.format(rsrcp=rsrc_path, prm=params)):
            logging.info('Execute action change for {rsrcp} \tparams: {prm}'.format(rsrcp=rsrc_path, prm=params))
            if TestToolkit.tested_api == ApiType.OPENAPI:
                params_list = params.split(' ')
                clock_date = params_list[0] if len(params_list) else ''
                clock_time = params_list[1] if len(params_list) > 1 else ''
                params = {'clock-date': clock_date, 'clock-time': clock_time}
            return SendCommandTool.execute_command(self.api_obj[TestToolkit.tested_api].action_change,
                                                   TestToolkit.get_engine(), rsrc_path, params)


class WebServerAPI(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/api')
        self.connections = BaseComponent(self, path='/connections')
        self.listen_address = BaseComponent(self, path='/listening-address')


class Api(MTLSableServerResource):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/api')
