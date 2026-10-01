import logging
import pytest
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import ApiType


cumulus_owner = "gosaini"


logger = logging.getLogger()

invalid_cmd_str = ['Invalid config', 'Error', 'command not found', 'Bad Request', 'Not Found', "unrecognized arguments",
                   "error: unrecognized arguments", "invalid choice", "Action failed", "Invalid Command",
                   "You do not have permission", "The requested item does not exist."]


@pytest.mark.checklist
@pytest.mark.cumulus
def test_stress_check_nvue(engines):
    logging.info("*********** CLI Stress Test ({}) *********** ".format(engines.dut.ip))
    num_of_iterations = 10

    cmd = 'nv show system version'
    logging.info("Run " + cmd)
    _run_cmd_nvue(engines, [cmd], num_of_iterations)
    logging.info(cmd + " succeeded -----------------------------------")

    cmd = 'nv show interface eth0 link'
    logging.info("Run " + cmd)
    _run_cmd_nvue(engines, [cmd], num_of_iterations)
    logging.info(cmd + " succeeded -----------------------------------")

    cmds = ['nv show platform firmware', 'nv show interface eth0 link']
    logging.info("Run " + cmds[0] + " and " + cmds[1])
    _run_cmd_nvue(engines, cmds, num_of_iterations)
    logging.info(cmds[0] + "and" + cmds[1] + " succeeded -----------------------------------")


@pytest.mark.checklist
@pytest.mark.cumulus
def test_stress_check_openapi(engines):
    logging.info("*********** REST Stress Test ({}) *********** ".format(engines.dut.ip))
    num_of_iterations = 45

    TestToolkit.tested_api = ApiType.OPENAPI

    cmd = 'openapi: show system version'
    system = System()
    logging.info("Run " + cmd)
    _run_cmd_openapi([system.version], num_of_iterations)
    logging.info(cmd + " succeeded -----------------------------------")

    cmd = 'openapi: show interface eth0 link'
    mgmt_int = Port('eth0')  # Explicitly specify eth0 as the port name
    logging.info("Run " + cmd)
    _run_cmd_openapi([mgmt_int.interface.link], num_of_iterations)  # Use interface.link to get the correct path
    logging.info(cmd + " succeeded -----------------------------------")

    cmds = ['nv show platform firmware', 'nv show interface eth0 link']
    platform = Platform()
    logging.info("Run openapi: " + cmds[0] + " and " + cmds[1])
    _run_cmd_openapi([platform.firmware, mgmt_int.interface.link], num_of_iterations)
    logging.info(cmds[0] + "and" + cmds[1] + " succeeded -----------------------------------")


def _run_cmd_nvue(engines, cmds_to_run, num_of_iterations):
    with allure.step("Run commands for {} iterations".format(num_of_iterations)):
        i = num_of_iterations
        try:
            while i > 0:
                for cmd in cmds_to_run:
                    logging.info("Run {} iterations of {}".format(cmd, i))
                    output = engines.dut.run_cmd(cmd)
                    if any(msg in output for msg in invalid_cmd_str):
                        raise Exception("FAILED - " + output)
                i -= 1
        except BaseException as ex:
            raise Exception("Failed during iteration #{}: {}".format(i, str(ex)))


def _run_cmd_openapi(obj_list, num_of_iterations):
    with allure.step("Run commands for {} iterations".format(num_of_iterations)):
        i = num_of_iterations
        try:
            while i > 0:
                logging.info("iteration #" + str(i))
                for obj in obj_list:
                    output = obj.show()
                    if any(msg in output for msg in invalid_cmd_str):
                        raise Exception("FAILED - " + output)
                i -= 1
        except BaseException as ex:
            raise Exception("Failed during iteration #{}: {}".format(i, str(ex)))
