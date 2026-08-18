import base64
import json
import logging
import os
import time
from datetime import datetime

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from devts.infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import ResultObj

logger = logging.getLogger()


class FilesTool:

    @staticmethod
    def get_subfiles_list(engine, folder_path, subfiles_pattern=""):
        """
        Get list of subfiles from a folder, optionally filtered by a pattern.

        Args:
            engine: The test engine to run commands on
            folder_path: Full path to the folder to list
            subfiles_pattern: Optional regex pattern to filter directories or files

        Returns:
            List of sub directories or files names that match the pattern

        Example:
            get_subfiles_list(engine, '/var/run/hw-management/ui/voltage', 'PMIC|PDB|HSC|FAN')
        """
        try:
            output = engine.run_cmd(f'find {folder_path} -mindepth 2')

            if not output.strip():
                return []

            # Extract just the directory names (remove the full path)
            dirs = [line.split('/')[-1] for line in output.splitlines() if line.strip()]

            # If no pattern specified, return all directories
            if not subfiles_pattern:
                return dirs

            # Filter directories based on the pattern
            if '|' in subfiles_pattern:
                # For OR patterns, use simple substring matching (much faster than regex)
                patterns = subfiles_pattern.split('|')
                return [dir_name for dir_name in dirs
                        if any(pattern in dir_name for pattern in patterns)]
            else:
                # For single patterns, use simple substring matching
                return [dir_name for dir_name in dirs
                        if subfiles_pattern in dir_name]

        except Exception as e:
            logger.info(f"Error getting subfiles from {folder_path}: {e}")
            return []

    @staticmethod
    def file_exists(engine, file_path):
        output = engine.run_cmd(f'ls {file_path}')
        return "No such file or directory" not in output

    @staticmethod
    def file_exists_sudo(engine, file_path: str) -> ResultObj:
        """
        Check if a file exists using 'test -f' with sudo privileges.

        This method is more robust than file_exists() because:
        - Uses 'test -f' which is purpose-built for file checking
        - Returns ResultObj for chaining with verify_result()
        - Works with protected files via sudo

        Args:
            engine: The test engine to run commands on
            file_path: Full path to the file to check

        Returns:
            ResultObj: Result with True if file exists, False if not

        Example:
            # Verify file exists
            FilesTool.file_exists_sudo(engines.dut, '/etc/ssh/principals/admin').verify_result(should_succeed=True)

            # Verify file does not exist
            FilesTool.file_exists_sudo(engines.dut, '/etc/ssh/principals/deleted_user').verify_result(should_succeed=False)
        """
        output = engine.run_cmd(f"sudo test -f {file_path} && echo 'EXISTS' || echo 'NOT_EXISTS'")
        file_exists = "NOT_EXISTS" not in output  # Avoid substring match bug

        info = f"File {file_path} {'exists' if file_exists else 'does not exist'}"
        return ResultObj(file_exists, info=info)

    @staticmethod
    def read_json_file(engine, file_path: str) -> dict:
        """Read and parse a JSON file from the DUT (via sudo); returns the parsed dict."""
        with allure.step(f"read json file {file_path}"):
            raw = engine.run_cmd(f"sudo cat {file_path}; echo")
            return OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()

    @staticmethod
    def read_text_file(engine, file_path: str) -> str:
        """Return the raw contents of *file_path* on the DUT ("" when it is absent).

        Unlike read_file_content, a missing file yields an empty string (via ``|| true``)
        instead of the cat error text, so callers can treat absence as "no content".
        """
        return engine.run_cmd(f"sudo cat {file_path} 2>/dev/null || true")

    @staticmethod
    def write_json_file(engine, file_path: str, data: dict) -> None:
        """Write *data* as JSON to *file_path* on the DUT (via sudo tee).

        The payload is base64-encoded so arbitrary JSON (single quotes, nested blobs)
        survives the shell without quoting bugs.
        """
        with allure.step(f"write json file {file_path}"):
            b64 = base64.b64encode(json.dumps(data).encode()).decode("ascii")
            engine.run_cmd(f"echo {b64} | base64 -d | sudo tee {file_path} > /dev/null")

    @staticmethod
    def wait_for_file(engine, file_path: str, timeout: int = 60, poll_interval: int = 3) -> None:
        """Poll until *file_path* exists on the DUT, else raise AssertionError."""
        with allure.step(f"wait up to {timeout}s for {file_path} to exist"):
            start = time.monotonic()
            while time.monotonic() - start < timeout:
                # ignore_result(): a missing file mid-poll is expected and must not
                # leave a failed ResultObj for the verify_result_objects teardown.
                if FilesTool.file_exists_sudo(engine, file_path).ignore_result():
                    return
                time.sleep(poll_interval)
            raise AssertionError(f"{file_path} was not (re)created within {timeout}s")

    @staticmethod
    def read_file_content(engine, file_path: str, use_sudo: bool = True) -> ResultObj:
        """
        Read the content of a file with optional sudo privileges.

        Note: Always returns success with content in returned_value. If the file doesn't exist
        or can't be read, the error message becomes the content. Callers should use
        verify_result(expected_value=...) to validate the content is as expected.

        Args:
            engine: The test engine to run commands on
            file_path: Full path to the file to read
            use_sudo: Whether to use sudo (default: True)

        Returns:
            ResultObj: Always returns True with file content (or error message) in returned_value

        Example:
            # Just read content
            content = FilesTool.read_file_content(engines.dut, '/etc/ssh/principals/admin').verify_result()

            # Read and verify content contains expected string
            FilesTool.read_file_content(
                engines.dut,
                '/etc/ssh/principals/admin'
            ).verify_result(expected_value='my-principal')
        """
        sudo_prefix = "sudo " if use_sudo else ""
        file_content = engine.run_cmd(f"{sudo_prefix}cat {file_path}")

        # Return content as-is. Caller uses verify_result(expected_value=...) to check correctness.
        # If file doesn't exist or can't be read, error message becomes the content,
        # which will fail content verification naturally.
        info = f"Read content from {file_path}"
        return ResultObj(True, returned_value=file_content, info=info)

    @staticmethod
    def validate_expected_files(engine, folder_path, expected_files, should_succeed=True):
        """
        :param engine:
        :param folder_path: folder full path
        :param expected_files: list of expected files or folders
        :param should_succeed:
        :return:
        """
        err_msg = ""
        with allure.step(f"validate all {expected_files} in {folder_path}"):
            output = engine.run_cmd(f'ls -l {folder_path}')
            output.splitlines()
            for file in expected_files:
                if file not in output:
                    err_msg += f"{file} file does not exist in the path {folder_path}"

        assert bool(err_msg) != should_succeed, err_msg if err_msg else ""
        return True

    @staticmethod
    def get_file_size_in_bytes(engine, file_path):
        output = engine.run_cmd(f'stat --format="%s" {file_path}')
        return int(output) if output.isdigit() else -1

    @staticmethod
    def fw_file_read(engine, file, fw_file_directory):
        cmd = "sudo cat " + fw_file_directory + "/" + file
        read_val = engine.run_cmd(cmd)
        return read_val

    @staticmethod
    def create_file_with_content(engine, file_name, file_type, content):
        """
        Create a file with content and upload to the DUT machine.

        Args:
            engine: The test engine
            file_name: Name of the file without extension
            file_type: File extension (e.g., 'json', 'yaml')
            content: Content to write to the file
        """

        # Create a temporary file locally
        local_file = f'/tmp/{file_name}.{file_type}'
        with open(local_file, 'w') as f:
            f.write(content)

        # Copy the file to the DUT machine
        dest_file = f'/tmp/{file_name}.{file_type}'
        engine.copy_file(source_file=local_file, dest_file=dest_file, file_system='/tmp', overwrite_file=True)

        # Clean up local file
        os.remove(local_file)

        return dest_file

    @staticmethod
    def run_prepare_expected_output_exit_code(engine: LinuxSshEngine, cmd: str, expected_output: str = "", expected_exit: str = "exit code 0") -> 'ResultObj':
        """
         Args:
           expected_exit: Either 'exit code 0' for successful execution, or 'other exit code <exit_code>' for failure checks.
           cmd: The shell command to execute on the remote machine.
           expected_output: The expected output from the command; used as a prefix for verification.
           engine: The engine object used to execute commands over SSH.

           Flow:
            1. Create a temporary log file on the target machine to capture all
            output (stdout and stderr) from the command execution.
            2. Construct and run a shell command that saves output to the log and appends exit code status.
            3. Read the log file contents.
            4. Verify if output and exit code are as expected.
            5. Return a ResultObj with the verification result and diagnostic info.
        """
        with TempFileOnEngine(engine, "txt") as temp_file:
            verifity_cmd = (
                f"set -o pipefail; "
                f"if {cmd} >> {temp_file.path} 2>&1; "
                f"then sudo printf 'exit code 0\n' >> {temp_file.path}; "
                f"else sudo printf 'other exit code %d\n' $? >> {temp_file.path}; fi"
            )
            logging.info(f"Running verify_command , cmd=: {cmd}")
            engine.run_cmd(verifity_cmd)
            exit_code_cmd_output = engine.run_cmd(f"sudo cat {temp_file.path}")
            logging.info(f"output of cmd and exit code 0 or other: {exit_code_cmd_output}")

        # double check (cmd output and exit code is as expected)
        output_flag, exit_code_flag = exit_code_cmd_output.startswith(expected_output), False
        debug_info = (f"cmd={cmd} ,exit_code_cmd_output =  {exit_code_cmd_output} "
                      f", expected_output = {expected_output} , expected_exit = {expected_exit}, output_flag = {output_flag} ")
        if output_flag:
            exit_code_flag = expected_exit in exit_code_cmd_output.replace(expected_output, "")
            debug_info += f", exit_code_flag = {exit_code_flag} "
        return ResultObj(exit_code_flag and output_flag, debug_info)

    @staticmethod
    def extract_tar_with_status_code(engine: LinuxSshEngine, archive_directory_path: str, filename: str) -> 'ResultObj':
        """
             Note: archive_directory_path should end with /
             flow:
                1. Construct the full archive file path by concatenating directory and filename
                2. Log the full archive file path for traceability
                3. Build the tar extraction command with sudo privileges
                4. Run FilesTool.run_prepare_expected_output_exit_code to execute the command, capture output and status, and verify the result
                5. Return a ResultObj with verification status and details
        """

        full_path = archive_directory_path + filename
        logging.info(f"Full archive path: {full_path}")
        with allure.step("Extract archive and verify result.   and also Read verification result and tar output"):
            return FilesTool.run_prepare_expected_output_exit_code(engine, f"sudo tar -xf {full_path} -C {archive_directory_path}")

    @staticmethod
    def cleanup_tmpfs(engines_dut, mount_point='/mnt/tmpfs', file_path='/mnt/tmpfs/testfile'):
        """Best-effort tmpfs cleanup. Never raises so that setup failures are not masked in finally."""
        engines_dut.run_cmd(f'sudo rm -f {file_path}')
        engines_dut.run_cmd(f'sudo umount {mount_point} || true')
        engines_dut.run_cmd(f'sudo rmdir {mount_point} || true')


class EngineFile:
    """
    Represents a file on the switch and provides methods to edit it. The file has to be already existing.
    The revert_to_original() method is useful for cleanup code.
    """

    def __init__(self, engine: ProxySshEngine, file_path: str):
        self.engine = engine
        self.file_path = file_path
        ls_output = engine.run_cmd("ls " + file_path)
        if ls_output.strip() != file_path.strip():
            raise ValueError(ls_output)

        with allure.step(f"Storing original content of file for later recovery: {file_path}"):
            self.original_content = self.get_content()
            logger.info(self.original_content)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.revert_to_original()

    def get_content(self) -> str:
        return self.engine.run_cmd("sudo cat " + self.file_path, print_output=False)

    def revert_to_original(self):
        self.replace_whole_content(self.original_content)
        new_content = self.get_content()
        if new_content != self.original_content:
            raise Exception(f"Failed to restore original content of {self.file_path}")

    def replace_whole_content(self, content: str):
        """Writes `content` to file, overwriting any existing content."""
        self.engine.run_cmd_set(["sudo su", f"cat > {self.file_path} << EOF\n{content}\nEOF", "exit"])

    def sed(self, pattern: str, new_text: str):
        self.engine.run_cmd(f"sudo sed -i 's/{pattern}/{new_text}/' {self.file_path}")
        logger.info(f"new content of {self.file_path}:\n" + self.get_content())

    def json_read(self):
        return json.loads(self.get_content())

    def json_overwrite(self, data):
        self.replace_whole_content(json.dumps(data, indent=4))


class TempFileOnEngine:
    """
    File is created on engine and automatically assigned a name. File is deleted at the end of usage. Usage example:
    with TempFileOnEngine(engines.dut) as f:
        logger.info(f.path)
        f.write('xyz')
    """

    def __init__(self, engine: ProxySshEngine, extension='test'):
        self.engine = engine
        self.path = datetime.now().strftime("/tmp/%Y%m%d%H%M%S.") + extension

    def __enter__(self):
        with allure.step(f"Creating temp file: {self.path}"):
            self.engine.run_cmd(f"touch {self.path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.engine.run_cmd(f"rm -f {self.path}")

    def write(self, text: str, newline=True, backslash_escapes=False):
        echo_args = '' if newline else ' -n'
        echo_args += ' -e' if backslash_escapes else ''
        self.engine.run_cmd(f"echo{echo_args} '{text}' >> {self.path}")
