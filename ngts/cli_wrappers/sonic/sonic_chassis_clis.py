import re
import logging
import json
from ngts.cli_wrappers.common.chassis_clis_common import ChassisCliCommon
from ngts.constants.constants import SonicConst
from ngts.cli_util.cli_parsers import generic_sonic_output_parser

logger = logging.getLogger()


class SonicChassisCli(ChassisCliCommon):
    """
    This class is for chassis cli commands for sonic only
    """

    def __init__(self, engine):
        self.engine = engine

    def get_platform(self):
        """
        This method execute command "show platform summary" and return the dut platform type
        :return: the dut platform type
        """
        output = self.show_platform_summary()
        pattern = r"Platform:\s*(.*)"
        try:
            platform = re.search(pattern, output, re.IGNORECASE).group(1)
            return platform
        except Exception:
            raise AssertionError("Could not match platform type for switch {}".format(self.engine.ip))

    def get_hwsku(self):
        """
        :return: the dut hwsku
        """
        hwsku = self.engine.run_cmd("sonic-cfggen -d -v DEVICE_METADATA.localhost.hwsku")
        return hwsku

    def show_platform_summary(self):
        """
        This method execute command "show platform summary" on dut
        :return: the cmd output
        """
        return self.engine.run_cmd("show platform summary")

    def show_platform_syseeprom(self):
        """
        This method execute command "show platform syseeprom" on dut
        :return: the cmd output
        """
        return self.engine.run_cmd("show platform syseeprom")

    def show_mst_status(self):
        return self.engine.run_cmd("sudo mst status")

    def get_pci_conf(self):
        return self.engine.run_cmd(SonicConst.ASIC_DETECT_GET_DEVICE_PATH_CMD).strip()

    def get_fw_info(self):
        return self.engine.run_cmd(f"sudo flint -d {self.get_pci_conf()} q")

    def parse_platform_summary(self):
        """
        Parse the output of "show platform summary"
        :return: dict, example: {'HwSKU': 'ACS-MSN4410', 'ASIC Count': '1', 'ASIC': 'mellanox'...}

        """
        platform_summary_dict = {}
        platform_summary = self.show_platform_summary()
        for line in platform_summary.splitlines():
            split_line = line.split(": ")
            platform_summary_dict.update({split_line[0]: split_line[1]})
        return platform_summary_dict

    def show_platform_fan(self):
        """
        This method execute command "show platform fan" on dut
        :return: the cmd output
        """
        fan_status = self.engine.run_cmd("show platform fan")

        fan_status_table_dict = generic_sonic_output_parser(fan_status,
                                                            headers_ofset=0,
                                                            len_ofset=1,
                                                            data_ofset_from_start=2,
                                                            data_ofset_from_end=-0,
                                                            column_ofset=2,
                                                            output_key='FAN')

        logger.info(f"fan status:{fan_status_table_dict}")
        return fan_status_table_dict

    def show_platform_psu_status(self):
        """
        This method execute command "show platform psustatus" on dut
        :return: the cmd output
        """
        psu_status = self.engine.run_cmd("show platform psustatus")
        psu_status_table_dict = generic_sonic_output_parser(psu_status,
                                                            headers_ofset=0,
                                                            len_ofset=1,
                                                            data_ofset_from_start=2,
                                                            data_ofset_from_end=-0,
                                                            column_ofset=2,
                                                            output_key='PSU')

        logger.info(f"psu status:{psu_status_table_dict}")
        return psu_status_table_dict

    def get_platform_hwsku(self):
        """
        This method execute command "show platform summary" and return the dut hwsku
        :return: the dut hwsku
        """
        output = self.show_platform_summary()
        hwsku_pattern = r"HwSKU:\s*(.*)"

        res = re.search(hwsku_pattern, output, re.IGNORECASE)
        if res:
            hwsku = res.group(1)
            logger.info(f"hwsku is {hwsku}")
            return hwsku
        else:
            raise Exception("Could not get hwsku for switch {}".format(self.engine.ip))

    def get_platform_json_data(self):
        """
        This method is to get the platform json data from the file of /usr/share/sonic/device/{platform}/platform.json
        :return: the platform json data
        """
        full_path_platform_json_in_dut = f"/usr/share/sonic/device/{self.get_platform()}/platform.json"
        dest_path = "/tmp/platform.json"
        logger.info(f"fetch platform.json from dut:{full_path_platform_json_in_dut}")
        self.engine.copy_file(source_file=full_path_platform_json_in_dut,
                              dest_file=dest_path,
                              file_system='/',
                              overwrite_file=True,
                              direction="get",
                              verify_file=False)

        with open(f"{dest_path}", "r") as f:
            platform_json_dict = json.load(f)
        logger.info(f"platform json dict:\n {platform_json_dict}")
        return platform_json_dict

    def get_supported_hwsku_platform_dir(self, platform=None):
        if platform is None:
            platform = self.get_platform()

        platform_dir = SonicConst.PLATFORM_FOLDER_PATH.format(PLATFORM=platform)
        supported_hwsku_dir = self.engine.run_cmd(
            f"find \"{platform_dir}\" -mindepth 2 -maxdepth 3 -type f -name hwsku.json -printf '%P\\n' | cut -d/ -f1 | sort -u"
        )
        logger.info(f"supported hwsku dir: {supported_hwsku_dir}")
        if not supported_hwsku_dir:
            raise Exception(f"No supported hwsku found for platform {platform}")
        return supported_hwsku_dir.splitlines()
