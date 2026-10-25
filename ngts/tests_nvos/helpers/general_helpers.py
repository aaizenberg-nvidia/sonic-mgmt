import logging
import sys
import pathlib
import base64
import random
import re
import shlex
import string
import subprocess
from typing import List, Union, Optional

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_tools.infra.ResultObj import ResultObj


def get_absolute_devts_path() -> Optional[str]:
    for path in sys.path:
        if pathlib.Path(path).stem == 'devts':
            return path


def verify_hidden_cmd_args_in_history(dut_engine: LinuxSshEngine, num_lines: int, cmd_prefix, interesting_args, forbidden_patterns: List[str] = []):

    history_cmd = f'history {num_lines}' if num_lines > 0 else 'history'
    history = dut_engine.run_cmd(history_cmd)

    # Step 1: Get all lines of commands that start with 'abc'
    relevant_commands_pattern = rf'^\s*\d+\s+{cmd_prefix}\s+(.+)$'
    relevant_commands = re.findall(relevant_commands_pattern, history, re.MULTILINE)

    arg_pattern = r'(\w+)\s+(\S+)'

    bad_commands = [
        f'{cmd_prefix} {command}' for command in relevant_commands
        if any(arg_val != '*' for arg_name, arg_val in dict(re.findall(arg_pattern, command)).items()
               if arg_name in interesting_args) or
        any(forbidden_pattern in command for forbidden_pattern in forbidden_patterns)
    ]

    assert not bad_commands, f'some commands history have no hidden args as expected:\n{bad_commands}'


def generate_rand_str(str_len, possible_chars=string.ascii_letters) -> str:
    return ''.join(random.choices(possible_chars, k=str_len))


def normalize_sensor_name(name: str) -> str:
    """Lowercase and strip non-alphanumeric characters. Used to match CLI
    sensor names (hyphenated, e.g. 'PMIC-3-ASIC1-DVDD-PL1-Out-2') against
    health-issue keys (space-separated), alarm resources ('PSU1') and sysfs
    directory names (plus-separated)."""
    return re.sub(r'[^a-z0-9]', '', name.lower())


def verify_result_obj_failure(result_obj: ResultObj, expected_err=None):
    result_obj.verify_result(False)
    if expected_err:
        assert expected_err in result_obj.info, f'err msg not as expected\nexpected: {expected_err}\nactual: {result_obj.info}'


def run_cmd(cmd: Union[str, list], timeout=10, validate: bool = True, stdout_func=logging.info) -> str:
    """
    Run given command on the player/running machine
    """
    cmd_str, cmd_list = (cmd, shlex.split(cmd)) if isinstance(cmd, str) else (
        ' '.join([str(item) for item in cmd]), cmd)

    stdout_func(f'run: {cmd_str}')
    # Run the bash script
    result = subprocess.run(cmd_list, capture_output=True, text=True, timeout=timeout)

    # Print the output
    stdout_func(result.stdout)

    # Print any error messages
    if validate and result.returncode != 0:
        stdout_func("Returned code is not 0. Errors:")
        stdout_func(result.stderr)
        raise ValueError(f'error has occurred\nout: {result.stdout}\nerr: {result.stderr}')

    return result.stdout


def run_ssh_cmd_with_rc(engine: LinuxSshEngine, cmd: str) -> tuple:
    """
    Run a command via SSH and capture both output and exit code in a single call.

    This avoids the common bug where 'echo $?' is run as a separate SSH command,
    which doesn't capture the exit code of the previous command.

    Uses base64 encoding to avoid special characters in the command causing
    netmiko output parsing issues (e.g., '?' is a regex meta character).

    Args:
        engine: SSH engine to run the command on
        cmd: Command to execute

    Returns:
        tuple: (output, exit_code) where output is the command output (str)
               and exit_code is the integer return code
    """

    marker = "EXIT_CODE_MARKER:"
    wrapped_cmd = f"{cmd}; echo {marker}$?"
    b64_cmd = base64.b64encode(wrapped_cmd.encode()).decode()
    safe_cmd = f"echo '{b64_cmd}' | base64 -d | sh"
    output = engine.run_cmd(safe_cmd)

    lines = output.strip().split("\n")
    exit_code_line = [line for line in lines if line.startswith(marker)]

    if exit_code_line:
        exit_code = int(exit_code_line[-1].replace(marker, ""))
        cmd_output = "\n".join(line for line in lines if not line.startswith(marker))
    else:
        logging.warning(f"Could not parse exit code from output: {output}")
        exit_code = -1
        cmd_output = output

    return cmd_output, exit_code
