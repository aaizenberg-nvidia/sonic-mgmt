import re
import logging
from typing import Dict
from datetime import datetime

from ngts.nvos_constants.constants_nvos import NvosConst, RemarkableLogsConsts, LogsSources
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.DefaultDict import DefaultDict
from ngts.constants.constants import BugHandlerConst
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


class BaseLog(BaseComponent):
    def __init__(self, parent_obj=None, path=''):
        super().__init__(parent=parent_obj, path=path)
        self.file = File(self)
        self.rotation = BaseComponent(self, path='/rotation')

    def rotate_logs(self):
        """
        Shared rotate_logs implementation for log rotation.
        """
        with allure.step('Rotate logs'):
            resource_path = self.get_resource_path()
            return SendCommandTool.execute_command(
                self.api_obj[TestToolkit.tested_api].action_rotate_logs,
                TestToolkit.get_engine(),
                resource_path
            ).get_returned_value()


class Log(BaseLog):
    def __init__(self, parent_obj=None):
        super().__init__(parent_obj=parent_obj, path='/log')
        self.component = Component(self)

    def write_to_log(self):
        with allure.step('Write content to logs'):
            return SendCommandTool.execute_command(self.api_obj[TestToolkit.tested_api].action_write_to_logs,
                                                   TestToolkit.get_engine()).get_returned_value()

    def verify_expected_logs(self, logs_to_find, logs_source=LogsSources.SYSLOG, engine=None, only_latest_log=False):
        """
        :param logs_source: could be syslog, nvued, user and auth
        :param logs_to_find: list of logs to find
        :param engine: system engine
        :param only_latest_log: verify in only latest log file, instead of all in log files.
        :return:
        """

        if not engine:
            engine = TestToolkit.get_engine()

        with allure.step('Verify expected logs'):

            log_search_errors: Dict[str, str] = {log: f'log "{log}" was not found' for log in logs_to_find}
            grep_logs = '|'.join(logs_to_find)

            if only_latest_log:
                log_files = [logs_source]
            else:
                with allure.step(f"Get all {logs_source} files"):
                    log_files = engine.run_cmd(f"ls {RemarkableLogsConsts.LOGS_PATH} | grep {logs_source}").splitlines()

            for log_file in log_files:
                if not log_search_errors:
                    break

                with allure.step(f"Try to find expected logs in {log_file}"):
                    output: str = engine.run_cmd(f'zgrep -a -E "{grep_logs}" {RemarkableLogsConsts.LOGS_PATH}{log_file}')

                if output:
                    for log in logs_to_find:
                        if re.search(log, output) and log in log_search_errors:
                            del log_search_errors[log]

            err = ',\n'.join(list(log_search_errors.values()))
            assert not log_search_errors, f"The following logs weren't found:\n{err}"

    def verify_expected_logs_by_time(self, logs_to_find, engine=None, only_latest_log=False, start_time=None):
        """
        :param logs_to_find: list of logs to find
        :param engine: system engine
        :param only_latest_log: verify in only latest log file, instead of all in log files.
        :param start_time: only logs printed after this time are relevant
        :return:
        """
        with allure.step('Verify expected logs'):
            log_search_errors: Dict[str, str] = {log: f'log "{log}" was not found' for log in logs_to_find}
            grep_logs = '|'.join(logs_to_find)

            if only_latest_log:
                log_files = ['syslog']
            else:
                log_files = OutputParsingTool.parse_json_str_to_dictionary(
                    self.file.show()).get_returned_value().keys()

            for log_file in log_files:
                if not log_search_errors:
                    break

                if log_file.endswith('.gz'):
                    cmd = 'zcat'
                else:
                    cmd = 'cat'
                output = engine.run_cmd(f'sudo {cmd} {RemarkableLogsConsts.LOGS_PATH}{log_file} | grep -E -a "{grep_logs}"')

                if output:
                    lines = output.splitlines()
                    for log in logs_to_find:
                        for line in lines:
                            matched_value = re.search(log, line)
                            if matched_value:
                                date_match = re.search(fr'{NvosConst.DATE_TIME_REGEX[0]}', line)
                                date_str = date_match.group()
                                parsed_time = datetime.strptime(date_str, BugHandlerConst.TIMESTAMP_FORMATS[3])
                                current_year = datetime.now().year
                                parsed_time = parsed_time.replace(year=current_year)
                                if start_time <= parsed_time and log in log_search_errors:
                                    del log_search_errors[log]
                                    break

            err = ',\n'.join(list(log_search_errors.values()))
            assert not log_search_errors, f"The following logs weren't found:\n{err}"


class File(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/file')
        self.file_id: Dict[str, FileId] = DefaultDict(
            lambda file_id: FileId(parent=self, file_id=file_id))

    def show_log(self, param='', exit_cmd='', expected_str='', exempted_err_msgs=()):
        """Show system log file. exempted_err_msgs: optional tuple of keywords to allow in output (e.g. 'Not Found')."""
        with allure.step('Show logs'):
            resource_path = self.get_resource_path()
            original_api = TestToolkit.tested_api
            try:
                TestToolkit.tested_api = ApiType.NVUE
                return SendCommandTool.execute_command_expected_str(
                    self.api_obj[TestToolkit.tested_api].show_log,
                    expected_str, TestToolkit.get_engine(), resource_path,
                    param, exit_cmd, exempted_err_msgs=exempted_err_msgs).get_returned_value()
            finally:
                TestToolkit.tested_api = original_api


class FileId(BaseComponent):
    def __init__(self, parent, file_id):
        super().__init__(parent=parent, path=f'/{file_id}')
        self.filename = file_id

    def action_upload(self, upload_path, expected_str="", dut_engine=None, should_succeed=True) -> bool:
        engine = dut_engine if dut_engine else TestToolkit.get_engine()
        resource_path = self.get_resource_path()
        with allure.step(f"Upload file {resource_path} to '{upload_path}'"):
            return SendCommandTool.execute_command_expected_str(
                self._cli_wrapper.action_deprecated, expected_str,
                engine, action_type='upload', resource_path=resource_path,
                param_name='remote-url', param_value=upload_path).get_returned_value(should_succeed)

    def action_delete(self, expected_str="", dut_engine=None, should_succeed=True) -> bool:
        engine = dut_engine if dut_engine else TestToolkit.get_engine()
        resource_path = self.get_resource_path()
        # Use system-specific action_delete so the file path (e.g. /var/log/audit/audit.log) is
        # passed as one token; the generic action_deprecated would replace '/' with ' ' and break it.
        with allure.step(f"Delete file: {resource_path}"):
            return SendCommandTool.execute_command_expected_str(
                self.api_obj[TestToolkit.tested_api].action_delete, expected_str,
                engine, resource_path, self.filename).get_returned_value(should_succeed)


class Component(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/component')
        self.component_id: Dict[str, ComponentId] = DefaultDict(
            lambda component_id: ComponentId(parent=self, component_id=component_id))


class ComponentId(BaseLog):
    def __init__(self, parent, component_id):
        super().__init__(parent_obj=parent, path=f'/{component_id}')
        self.level = BaseComponent(self, path='/level')
