import datetime
import itertools
import os
import re
from typing import List

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from ngts.tests_nvos.general.security.security_test_tools.constants import AaaServerLocation

DEFAULT_ACCOUNTING_FILE_PATH = '/var/log/tac.acct'


class AaaAccountingLog:
    def __init__(self, log_row: str) -> None:
        try:
            row_split = log_row.split('cmd=')
            self.cmd: str = row_split[1]

            rest_of_row = row_split[0]
            values = re.split(r'\s+|\t+', rest_of_row)

            self.date: str = ' '.join(values[0:2])
            self.time: str = values[2]
            self.client_ip: str = values[3]
            self.username: str = values[4]
            # self.something = values[5]
            self.client_dn: str = values[6]
            self.op_status: str = values[7]  # start/stop
            self.timestamp = values[8]  # start time
            self.task_id = values[9]
            # self.service = values[10]
        except Exception as e:
            raise Exception(f'Log row does not have the expected format.\nError: {e}')


class AaaAccountingLogsFileContent:
    DATETIME_FORMAT = "%b %d %H:%M:%S"

    def __init__(self, raw_content: str) -> None:
        self.raw_content = raw_content
        split_rows: List[str] = raw_content.split('\n')
        self.logs: List[AaaAccountingLog] = [AaaAccountingLog(row) for row in split_rows if row != ""]

    def remove_logs_before_time(self, time: str):
        time_obj = datetime.datetime.strptime(time, AaaAccountingLogsFileContent.DATETIME_FORMAT)
        filtered_logs: List[AaaAccountingLog] = []
        for log in self.logs:
            log_datetime = datetime.datetime.strptime(' '.join([log.date, log.time]),
                                                      AaaAccountingLogsFileContent.DATETIME_FORMAT)
            if log_datetime >= time_obj:
                filtered_logs.append(log)
        self.logs = filtered_logs


class AaaServerManager:
    def __init__(self, ip: str, server_docker_name: str = '') -> None:
        self.ip = ip
        self.server_docker_name: str = server_docker_name
        # The host running the AAA docker containers differs per lab, so the SSH
        # login differs too (Yokneam -> TEST_SERVER_*, Mevo Carmel -> VM_*).
        user_env, password_env = AaaServerLocation.host_ssh_creds_env_for_address(ip)
        self.server_engine = LinuxSshEngine(ip, os.getenv(user_env), os.getenv(password_env))

    def __assert_ip(self):
        assert self.ip, 'Tried to make operation with manager of empty ip!'

    def __op_on_accounting_log_file(self, cmd: str) -> str:
        if self.server_docker_name:
            cmd = f'docker exec {self.server_docker_name} {cmd}'

        return self.server_engine.run_cmd(cmd)

    def __show_op_on_accounting_log_file(self, accounting_file_path: str, show_cmd: str, grep: List[str] = None,
                                         after_time: str = '') -> AaaAccountingLogsFileContent:
        if grep:
            greps = [g.replace('.', '\\.') for g in grep]    # replace dot for ip address
            greps_permutations = [list(perm) for perm in itertools.permutations(greps)]  # order doesn't matter
            greps = ['.*'.join(perm) for perm in greps_permutations]
            grep_pattern = '|'.join(greps)  # or between all greps permutations
            cmd = f'grep -E "{grep_pattern}" {accounting_file_path} | {show_cmd}'
        else:
            cmd = f'{show_cmd} {accounting_file_path}'

        # if grep:
        #     cmd = f'grep -E "{grep[0]}'
        #     for pattern in grep[1:]:
        #         cmd += f'|{pattern}'
        #     cmd += f'" {accounting_file_path} | {show_cmd}'
        # else:
        #     cmd = f'{show_cmd} {accounting_file_path}'

        # cmd = f'{show_cmd} {accounting_file_path}'
        # if grep:
        #     for gr in grep:
        #         cmd = f'{cmd} | grep -E "{gr}"'

        # if after_time:
        #     # cmd = f"{cmd} | awk '/{after_time}/" + "{p=1}p'"
        #     awk_cmd = f'awk -v target_time="{after_time}" ' + "'{if ($0 >= target_time || p) {print; p=1}}'"
        #     cmd = f'{cmd} | {awk_cmd}'
        logs = AaaAccountingLogsFileContent(self.__op_on_accounting_log_file(cmd))
        if after_time:
            logs.remove_logs_before_time(after_time)

        return logs

    def cat_accounting_logs(self, accounting_file_path: str = DEFAULT_ACCOUNTING_FILE_PATH, grep: List[str] = None,
                            after_time: str = '') -> AaaAccountingLogsFileContent:
        self.__assert_ip()
        return self.__show_op_on_accounting_log_file(accounting_file_path, 'cat', grep, after_time)

    def tail_accounting_logs(self, accounting_file_path: str = DEFAULT_ACCOUNTING_FILE_PATH, grep: List[str] = None,
                             after_time: str = '') -> AaaAccountingLogsFileContent:
        self.__assert_ip()
        return self.__show_op_on_accounting_log_file(accounting_file_path, 'tail', grep, after_time)

    def clear_accounting_logs(self, accounting_file_path: str = DEFAULT_ACCOUNTING_FILE_PATH) -> str:
        self.__assert_ip()
        # return self.__op_on_accounting_log_file(accounting_file_path, 'rm -f')  # remove file
        return self.__op_on_accounting_log_file(f'bash -c "echo -n > {accounting_file_path}"')  # clear content
