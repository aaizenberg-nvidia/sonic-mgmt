import logging
import re
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from ngts.constants.constants import BugHandlerConst
from ngts.nvos_constants.constants_nvos import CumulusConsts, NvosConst, SystemConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.FilesTool import FilesTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.system.Files import Files
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


class TechSupport(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path="/tech-support")
        self.files = Files(self)
        self.file_name = ""

    def action_delete(self, file_name):
        with allure.step("Delete tech-support: {}".format(file_name)):
            return SendCommandTool.execute_command(
                self.api_obj[TestToolkit.tested_api].action_delete, TestToolkit.engines.dut, self.get_resource_path(), file_name
            )

    def action_generate(self, engine="", option="", since_time="", test_name="", verify_size=False):
        """
        in the future the command will be nv action generate system tech-support (without files)
        changes to do :
            update self._resource_path in the init method
            remove self.get_resource_path().replace('/files', ' ') in this method
        """
        with allure.step("Execute action for {resource_path}".format(resource_path=self.get_resource_path())):
            if not engine:
                engine = TestToolkit.get_engine()

            cmd_out, duration = OperationTime.save_duration(
                "generate tech-support",
                option,
                test_name,
                SendCommandTool.execute_command,
                self.api_obj[TestToolkit.tested_api].action_generate_techsupport,
                engine,
                self.get_resource_path(),
                option,
                since_time,
            )
            cmd_out.ignore_result()
            if "failed" in cmd_out.info or "error" in cmd_out.info:
                return cmd_out.info, duration
            tech_support_folder = str(self.parse_techsupport_folder_name(cmd_out))

            if verify_size:
                self.verify_size(engine, tech_support_folder)

            return tech_support_folder, duration

    def verify_size(self, engine, tech_support_folder: str = "", device=None):
        """
        Verify that tech-support folder size is within the expected limit.

        Args:
            engine: SSH engine to run commands on
            tech_support_folder: Path to tech-support folder (uses self.file_name if not provided)
            device: Device object (uses TestToolkit.get_device() if not provided)

        Raises:
            ValueError: If no tech_support_folder provided and self.file_name is empty
        """
        if not tech_support_folder:
            if not self.file_name:
                raise ValueError("tech_support_folder not provided and self.file_name is empty")
            tech_support_folder = str(PurePosixPath(SystemConsts.TECHSUPPORT_FILES_PATH) / self.file_name)
        if not device:
            device = TestToolkit.get_device()

        with allure.step("Verify tech-support file size"):
            # Round output to MB by -m flag and trim white spaces with column to receive int like output
            output = engine.run_cmd(f"sudo du -sm {tech_support_folder} | column -t")
            size_in_MB = int(output.split(" ")[0])
            size_limit = device.constants.techsupport_size_limit_mb
            logger.info(f"Tech-support size: {size_in_MB}MB (limit: {size_limit}MB)")
            assert size_in_MB < size_limit, f"{tech_support_folder} size ({size_in_MB}MB) should be less than {size_limit}MB"

    def action_upload(self, upload_path, file_name):
        with allure.step("Upload techsupport {file} to '{path}".format(file=file_name, path=upload_path)):
            return SendCommandTool.execute_command(
                self.api_obj[TestToolkit.tested_api].action_upload,
                TestToolkit.get_engine(),
                path=self.get_resource_path() + "/files",
                file_name=file_name,
                url=upload_path,
            )

    def parse_techsupport_folder_name(self, techsupport_res) -> PurePosixPath:
        """Extract and return the generated archive path."""
        archive_suffixes = (".tar.gz", ".txz")
        # Cumulus example:
        # Generated tech-support
        # Please open a support case from https://www.nvidia.com/en-us/support/enterprise/
        #        to send the support file /var/support/cl_support_cumulus_20260916_182535.txz
        # Please check the Contact Enterprise Support section for methods to open a support case.
        output_formats = (
            ("Generated tech-support", PurePosixPath(SystemConsts.TECHSUPPORT_FILES_PATH)),
            ("Please send", PurePosixPath(CumulusConsts.TECHSUPPORT_FILES_PATH)),
            ("to send the support file", PurePosixPath(CumulusConsts.TECHSUPPORT_FILES_PATH)),
        )

        for line in techsupport_res.returned_value.splitlines():
            for output_marker, tech_support_path in output_formats:
                if output_marker not in line:
                    continue
                output = line.partition(output_marker)[2]
                for token in output.split():
                    archive_path = PurePosixPath(token.strip("\"'(),:;."))
                    if str(archive_path).endswith(archive_suffixes):
                        self.file_name = archive_path.name
                        return archive_path if archive_path.is_absolute() else tech_support_path / archive_path

        raise ValueError(f"Unable to parse tech-support archive name from output: {techsupport_res.returned_value!r}")

    def extract_techsupport_files(self, engine, file_name=""):
        self.file_name = file_name if file_name else self.file_name
        with allure.step(f"extract {self.file_name}"):
            logging.info(f"extract {self.file_name}")
            FilesTool.extract_tar_with_status_code(engine, SystemConsts.TECHSUPPORT_FILES_PATH, self.file_name).verify_result()

    def extract_techsupport_subfile(self, engine, sub_folder, filename, tech_support_dir=""):
        """
        Extract a specific file within the tech-support structure

        :param engine: engine
        :param sub_folder: sub folder within tech-support (e.g., 'hw-mgmt')
        :param filename: filename to extract (e.g., 'hw-mgmt-dump.tar.gz')
        :param tech_support_dir: tech-support directory name (optional, uses self.file_name if not provided)
        """
        if not tech_support_dir:
            tech_support_dir = self.file_name.replace(".tar.gz", "")

        with allure.step(f"extract {filename} from {sub_folder}"):
            logging.info(f"extract {filename} from {sub_folder}")
            full_path = f"{tech_support_dir}/{sub_folder}"
            engine.run_cmd(f"sudo tar -xf {full_path}/{filename} -C {full_path}")

    def get_techsupport_files_names(self, engine, expected_files_dict):
        """
        :param engine:
        :param expected_files_dict: the files expected to be in the techsupport .tar.gz
        :return: dict, dict item for example - {sub-folder : list of files contained in that sub-folder)
        """
        with allure.step("Get all tech-support files"):
            logging.info("Get all tech-support files")
            full_path = SystemConsts.TECHSUPPORT_FILES_PATH + self.file_name.replace(".tar.gz", "")
            dict_files = {}
            for sub_folder in expected_files_dict.keys():
                dict_files[sub_folder] = engine.run_cmd("ls " + full_path + "/" + sub_folder).split()
            return dict_files

    def get_techsupport_empty_files(self, engine, test_support_file="", tech_folder=""):
        """
        :param engine: engine
        :param tech_folder: the tech_folder sub folder in techsupport .tar.gz
        :return: list of the empty files in the tech-support sub folder
        """
        self.file_name = test_support_file if test_support_file else self.file_name
        with allure.step(f"Get all tech-support empty files from {tech_folder}"):
            logging.info(f"Get all tech-support empty files from {tech_folder}")
            full_path = SystemConsts.TECHSUPPORT_FILES_PATH + self.file_name.replace(".tar.gz", "")
            output = engine.run_cmd("sudo find " + full_path + "/" + tech_folder + " -type f -empty")
            return [file.split("/")[-1] for file in output.split()]

    def cleanup(self, engine):
        engine.run_cmd("sudo rm -rf " + SystemConsts.TECHSUPPORT_FILES_PATH + self.file_name.replace(".tar.gz", ""))

    def clean_timestamp_techsupport_sdk_files_names(self, file_names):
        return [self.rename_file(name) for name in file_names]

    def rename_file(self, filename):
        sai_sdk_regex = re.compile(r"sai_sdk_dump_\d{2}_\d{2}_\d{4}_\d{2}_\d{2}_(AM|PM)[._](.*)")
        sdk_dump_ext_regex = re.compile(r"(sdk_dump_ext)_.*?_(dev\d+.*)")
        if sai_sdk_regex.match(filename):
            return sai_sdk_regex.sub(r"sai_sdk_dump.\2", filename)
        elif sdk_dump_ext_regex.match(filename):
            return sdk_dump_ext_regex.sub(r"\1_\2", filename)
        return filename

    def get_techsupport_files_list(self, engine, tech_folder):
        """
        :param engine:
        :param tech_folder: :param tech_folder: the tech_folder sub folder in techsupport .tar.gz
        :return: list of files contained in that sub-folder)
        """
        with allure.step(f"Get all tech-support files from {tech_folder}"):
            logging.info(f"Get all tech-support files from {tech_folder}")
            full_path = SystemConsts.TECHSUPPORT_FILES_PATH + self.file_name.replace(".tar.gz", "")
            output = engine.run_cmd("ls " + full_path + "/" + tech_folder)
            return output.split()

    def bundle_contains_file(self, engine, file_name, tech_support_dir=''):
        """
        Check whether a file appears anywhere under the extracted tech-support tree.

        Depth-agnostic: matches by name at any depth below the dump directory, since
        the exact in-bundle location of an artifact is not guaranteed.

        :param engine: engine
        :param file_name: file name to look for (e.g. '47-otel-syslog.conf')
        :param tech_support_dir: extracted dump directory (defaults to the last generated one)
        :return: True if the file is present in the bundle
        """
        if not tech_support_dir:
            tech_support_dir = SystemConsts.TECHSUPPORT_FILES_PATH + self.file_name.replace('.tar.gz', "")
        with allure.step(f'Check tech-support bundle for {file_name}'):
            output = engine.run_cmd(f'sudo find {tech_support_dir} -name {file_name}')
            return bool(output.strip())

    def check_techsupport_file_age(self, engine, system, tech_support_path: str = '', max_age_hours: int = 24):
        """
        Verify that tech-support file was generated within a specified number of hours.

        :param engine: system engine
        :param system: System() object
        :param tech_support_path: Path to the tech-support file.
        :param max_age_hours: Maximum allowed age of the tech-support file in hours. Default is 24 hours.
        """
        with allure.step(f"Run 'stat {tech_support_path}' and get tech-support file's birth-time"):
            file_info = engine.run_cmd(f"stat {tech_support_path}")
            birth_time_match = re.search(rf"Birth: ({NvosConst.DATE_TIME_REGEX[1]})", file_info)
            birth_time = datetime.strptime(birth_time_match.group(1), BugHandlerConst.TIMESTAMP_FORMATS[4])

            with allure.step("Verify last tech-support file was generated in less than 24 hours ago"):
                current_time_str = ClockTools.get_local_time_from_show_system_date_time_output(system.datetime.show())
                current_time = datetime.strptime(current_time_str, BugHandlerConst.TIMESTAMP_FORMATS[4])
                assert current_time - birth_time < timedelta(hours=max_age_hours), (
                    f"The last tech-support file was generated {birth_time}, more than 24 hours ago"
                )
