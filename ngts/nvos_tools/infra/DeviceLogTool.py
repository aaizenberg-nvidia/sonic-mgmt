import logging
import time
from datetime import datetime
from typing import List, Union

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_constants.constants_nvos import SyslogConsts
from ngts.tests_nvos.system.stats.constants import StatsConsts
from ngts.nvos_tools.infra import ExceptionTool
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


def grep_log_lines_after_datetime(engine: LinuxSshEngine, expression: str, start_time: Union[datetime, str],
                                  log_files=(SyslogConsts.SYSLOG_LOG_PATH, SyslogConsts.SYSLOG_LOG_PATH + '.1'),
                                  case_sensitive=False, extended_regex=False
                                  ) -> List[str]:
    """start_time should be either a datetime object or a string like '2020-12-31 19:00:00' """
    with allure.step(f'Searching logs after {start_time} for {repr(expression)}'):
        if isinstance(start_time, datetime):
            start_time = start_time.strftime(StatsConsts.SYSTEM_TIME_FORMAT)
        else:
            try:
                datetime.strptime(start_time, StatsConsts.SYSTEM_TIME_FORMAT)
            except ValueError:
                raise ValueError(f'{start_time=} but should be in format {StatsConsts.SYSTEM_TIME_FORMAT}')

        log_lines = []
        # -a is required: syslog may contain NUL bytes (ANSI progress output of "nv action" commands),
        # and without it grep reports "binary file matches" and drops every match after the first NUL.
        grep_flags = f'-a{"" if case_sensitive else "i"}{"E" if extended_regex else "e"}'
        escaped_expression = "'" + expression.replace("'", r"'\''") + "'"

        # grep all lines with expression
        for f in reversed(log_files):
            try:
                log_lines += engine.run_cmd(f'grep {grep_flags} {escaped_expression} {f} 2> /dev/null').splitlines()
            except AssertionError as e:  # grep failed - maybe file doesn't exist
                ExceptionTool.log_exception(e)
                continue

        for i, line in enumerate(reversed(log_lines)):
            try:
                timestamp = ClockTools.get_datetime_of_system_log_line(line)
            except ValueError:  # log-line does not begin with a timestamp
                continue
            if timestamp < start_time:
                output = log_lines[-i:] if i > 0 else []
                break
        else:  # if no break happened
            output = log_lines
        allure.attach(f'Found log lines with {repr(expression)}', '\n'.join(output))
        return output


def mark_syslog(engine: LinuxSshEngine, marker: str | None = None, tag: str = 'ngts') -> str:
    """Write a marker line (unique when not given) to the DUT syslog and return it."""
    marker = marker or f'ngts-marker-{time.time_ns()}'
    engine.run_cmd(f"sudo logger -t {tag} '{marker}'")
    return marker


def count_log_lines_after_marker(engine: LinuxSshEngine, marker: str, expression: str,
                                 log_files=(SyslogConsts.SYSLOG_LOG_PATH + '.1', SyslogConsts.SYSLOG_LOG_PATH)) -> int:
    """Count log lines matching ``expression`` (grep -E) written after the ``marker`` line."""
    output = engine.run_cmd(f"sudo cat {' '.join(log_files)} 2>/dev/null"
                            f" | awk 'index($0, \"{marker}\") {{seen=1; next}} seen' | grep -acE '{expression}'")
    return int(output.strip().splitlines()[-1])
