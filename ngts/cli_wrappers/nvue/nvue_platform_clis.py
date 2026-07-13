import logging

from ngts.cli_wrappers.nvue.nvue_base_clis import NvueBaseCli, check_output
from ngts.nvos_constants.constants_nvos import ActionType

logger = logging.getLogger()


class NvuePlatformCli(NvueBaseCli):

    def __init__(self):
        self.cli_name = "Platform"

    @staticmethod
    @check_output
    def action_generate(engine, resource_path, name=""):
        return NvuePlatformCli.action_deprecated(engine, action_type=ActionType.GENERATE.replace('@', ''), resource_path=resource_path, param_value=name)

    @staticmethod
    @check_output
    def action_fetch_firmware(engine, resource_path, remote_url):
        path = resource_path.replace('/', ' ')
        cmd = "nv action fetch {} {}".format(path, remote_url)
        logging.info("Running '{cmd}' on dut using NVUE".format(cmd=cmd))
        return engine.run_cmd(cmd)
