import logging
import re
import pytest

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.CommandNodeTree import CommandNodeTree, strip_dangling_quotes
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.mark.simx
@pytest.mark.general
def test_show_commands_validate(engines, random_api):
    """
    @summary:
    Basic validation all show commands

    @steps:
    1. Generate list of commands via CLI
    2. Create data structure from the list of commands
    3. Create a list of all show commands
    4. Execute all show commands which does not need a parameter
    5. Do basic validation of the output

    @param test_api: The API type to use (from ApiType enum)
    @param engines: Object giving access to DUT shell/command interfaces
    """
    command_tree = CommandNodeTree()

    try:
        with allure.step("Create tree data structure from the list of commands"):
            command_tree.create_command_list(engines, exclude_cmds=['<'])
            command_tree.build_tree()
            logger.info(f"Created command tree with {len(command_tree.command_list)} commands")

        with allure.step("Create a list of all show commands"):
            command_start_str = "nv show"
            cmd_node = command_tree.get_node_by_command(command_start_str)
            # get the list of commands after nv show <cmd>
            show_cmd_list = command_tree.find_paths(cmd_node)
            # create list of full show commands
            show_cmd_list = [(command_start_str + " " + show_cmd) for show_cmd in show_cmd_list]
            logger.info("\nShow command list:\n")
            output = ""
            for show_cmd in show_cmd_list:
                output = output + "\n" + show_cmd
            logger.info("{}".format(output))

        with allure.step("Validate all show commands and check if syslog lines are added"):
            cmd_syslog_lines_dict = {}
            cmd_nvued_log_lines_dict = {}
            for show_cmd in show_cmd_list:
                full_show_cmd = show_cmd + " -o json"
                syslog_lines_before = helper_get_syslog_line_count(engines)
                nvued_log_lines_before = helper_get_syslog_line_count(engines, logs_file="nvued.log")
                OutputParsingTool.parse_json_str_to_dictionary(engines.dut.run_cmd(full_show_cmd)).verify_result()
                syslog_lines_after = helper_get_syslog_line_count(engines)
                nvued_log_lines_after = helper_get_syslog_line_count(engines, logs_file="nvued.log")
                cmd_syslog_lines_dict[full_show_cmd] = syslog_lines_after - syslog_lines_before
                cmd_nvued_log_lines_dict[full_show_cmd] = nvued_log_lines_after - nvued_log_lines_before
            logger.info(f"Syslog lines added per show command: {cmd_syslog_lines_dict}")
            logger.info(f"Nvued log lines added per show command: {cmd_nvued_log_lines_dict}")
            with allure.independent_step("check syslog lines threshold"):
                max_syslog_lines_per_command = 10
                commands_over_syslog_threshold = [
                    cmd for cmd, syslog_lines in cmd_syslog_lines_dict.items()
                    if syslog_lines > max_syslog_lines_per_command
                ]
                assert len(commands_over_syslog_threshold) == 0, \
                    f"Commands added more than {max_syslog_lines_per_command} syslog lines: " \
                    f"{commands_over_syslog_threshold}"
            with allure.independent_step("check nvued log lines threshold"):
                max_nvued_log_lines_per_command = 40
                commands_over_nvued_log_threshold = [
                    cmd for cmd, nvued_log_lines in cmd_nvued_log_lines_dict.items()
                    if nvued_log_lines > max_nvued_log_lines_per_command
                ]
                assert len(commands_over_nvued_log_threshold) == 0, \
                    f"Commands added more than {max_nvued_log_lines_per_command} nvued log lines: " \
                    f"{commands_over_nvued_log_threshold}"

    finally:
        with allure.step("Validated all show commands"):
            logger.info("Completed basic validation of all show commands which does not need parameters")


@pytest.mark.simx
@pytest.mark.general
def test_commands_help_str_validate(engines, random_api):
    """
    @summary:
    Basic validation all show commands

    @steps:
    1. Generate list of commands via CLI
    2. Create data structure from the list of commands
    3. Create a list of all show commands
    4. Execute all show commands which does not need a parameter
    5. Do basic validation of the output

    @param test_api: The API type to use (from ApiType enum)
    @param engines: Object giving access to DUT shell/command interfaces
    """
    command_tree = CommandNodeTree()

    try:
        with allure.step("Create tree data structure from the list of commands"):
            command_tree.create_command_list(engines, exclude_cmds=[], prune_cmds=['<', '(', '['])
            command_tree.build_tree()
            logger.info(f"Created command tree with {len(command_tree.command_list)} commands")

        with allure.step("Create list of all commands from the command tree"):
            cmd_list = command_tree.find_paths()

        with allure.step("Validate help string for each command"):
            cmd_issues_dict = {}
            for cmd in cmd_list:
                if "certificate self-signed" in cmd:  # to ignore an issue in discussion
                    continue
                if "system health history files" in cmd:  # to ignore an issue in discussion
                    continue
                help_str_cmd = cmd + " --help"
                help_str = engines.dut.run_cmd(help_str_cmd)
                result, error_list = helper_help_str_validate(help_str)
                if not result:
                    cmd_issues_dict[f'{cmd}'] = f'{error_list}'
            assert len(cmd_issues_dict) is 0, f"Issue with help strings:{cmd_issues_dict}"

    finally:
        with allure.step("Validated help string for all commands"):
            logger.info("Completed basic validation of help string for all commands which does not need parameters")


@pytest.mark.simx
@pytest.mark.general
def test_commands_help_str_attr_validate(engines, random_api):
    """
    @summary:
    Basic validation all show commands

    @steps:
    1. Generate list of commands via CLI
    2. Create data structure from the list of commands
    3. Create a list of all show commands
    4. Execute all show commands which does not need a parameter
    5. Do basic validation of the output

    @param test_api: The API type to use (from ApiType enum)
    @param engines: Object giving access to DUT shell/command interfaces
    """
    command_tree = CommandNodeTree()

    try:
        with allure.step("Create tree data structure from the list of commands"):
            command_tree.create_command_list(engines)
            command_tree.command_list = alter_command_list_for_attribute_validation(command_tree.command_list)
            command_tree.build_tree()
            logger.info(f"Created command tree with {len(command_tree.command_list)} commands")

        with allure.step("Validate help string for each command"):
            cmd_issues_dict = {}
            for cmd in command_tree.command_list:
                if "nv unset system syslog format" in cmd:  # to ignore an issue in discussion
                    continue
                help_str_cmd = cmd + " --help"
                help_str = engines.dut.run_cmd(help_str_cmd)
                result, error_msg = helper_help_str_attribute_validate(help_str, cmd, command_tree)
                if not result:
                    cmd_issues_dict[f'{cmd}'] = f'Attributes do not match: {error_msg}'
            assert len(cmd_issues_dict) is 0, f"Issue with help strings attributes:{cmd_issues_dict}"

    finally:
        with allure.step("Validated help string for all commands"):
            logger.info("Completed basic validation of help string for all commands which does not need parameters")


def helper_help_str_validate(help_str):
    match = re.search(r'usage:(.+?)Description:\n(.+?)\n', help_str, re.DOTALL)
    error_list = []
    result = True
    if match is None:
        logger.info(f'Help string does not have required fields')
        error_list.append('Does not have required fields')
        return False, error_list
    usage_str = match.group(1)
    description_str = match.group(2)
    if len(usage_str) < 10:
        error_list.append(f'Usage string is very small- {usage_str}')
        result = False
    if len(description_str) < 10:
        error_list.append(f'Description string is very small- {description_str}')
        result = False

    return result, error_list


def helper_get_syslog_line_count(engines, logs_file="syslog"):
    assert re.match(r'^[A-Za-z0-9_.-]+$', logs_file), f"Invalid log file name: {logs_file}"
    rotated_logs_file = helper_get_rotated_log_file(logs_file)
    log_file_paths = f"/var/log/{logs_file} /var/log/{rotated_logs_file}"
    log_line_count_cmd = (
        "sudo sh -c 'total=0; "
        f"for file in {log_file_paths}; do "
        "if [ -f \"$file\" ]; then lines=$(wc -l < \"$file\"); total=$((total + lines)); fi; "
        "done; echo $total'"
    )
    line_count_output = engines.dut.run_cmd(log_line_count_cmd).strip()
    match = re.search(r'(\d+)$', line_count_output)
    assert match is not None, f"Failed to get {logs_file} line count: {line_count_output}"
    return int(match.group(1))


def helper_get_rotated_log_file(logs_file):
    log_file_name_parts = logs_file.rsplit('.', 1)
    if len(log_file_name_parts) == 2:
        return f"{log_file_name_parts[0]}.1.{log_file_name_parts[1]}"
    return f"{logs_file}.1"


def helper_help_str_attribute_validate(help_str, cmd, command_tree):
    attr_list = helper_help_str_attributes_get(help_str)
    if len(attr_list) == 0:
        return True, ""
    attr_list_cmd_tree = command_tree.get_node_by_command(cmd).sub_command.keys()
    if sorted(attr_list) != sorted(attr_list_cmd_tree):
        return False, f"{attr_list} instead of {attr_list_cmd_tree}"
    return True, ""


def helper_help_str_attributes_get(help_str):
    attribute_list = []
    match = re.search(r'Attributes:\n(.+)', help_str, re.DOTALL)
    if match:
        attributes_str = match.group(1)
        attributes_str_lines = attributes_str.splitlines()
        for line in attributes_str_lines:
            if line == "":
                # End of Attributes section of the help string
                break
            if len(line) >= 3 and line[0:3] == "   ":
                # This is not an attribute name but continuation of previous attribution description
                continue
            if line:
                attribute_list.append(line.strip().split()[0])
    return attribute_list


def alter_command_list_for_attribute_validation(command_list):
    new_command_list = []
    for command in command_list:
        if '[' in command:
            # Form commands with all attributes and add to the command list as separate commands
            matches = re.findall(r"\[(.*?)\]", command)
            command = command.split('[')[0].strip()
            for match in matches:
                attr = match.split(' ')[0]
                command_with_attr = command + " " + attr
                if command_with_attr not in command_list:
                    command_list.append(command_with_attr)
        if '(' in command:
            command = " ".join(command.split('(')[0].split(' ')[:-1])
        if '<' in command:
            command = strip_dangling_quotes(command.split('<')[0])
        if command not in new_command_list:
            new_command_list.append(command)
    return new_command_list
