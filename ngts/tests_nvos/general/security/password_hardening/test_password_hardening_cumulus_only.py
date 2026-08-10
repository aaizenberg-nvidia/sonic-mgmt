import random
import re
import string

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import TestFlowType
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.System import *
from ngts.nvos_tools.system.User import User
from ngts.tests_nvos.general.security.password_hardening.PwhConsts import PwhConsts
from ngts.tests_nvos.general.security.password_hardening.PwhTools import PwhTools
from ngts.tests_nvos.general.security.security_test_tools.constants import AaaConsts


cumulus_owner = "lharo"


@pytest.mark.cumulus_only
@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.simx_security
def test_password_hardening_disable_state_issue(engines, system, testing_users):
    """
    Check functionality when feature is disabled.
    When disable, cannot change root PW
    Bug id: 4025350
    Steps:
    1. Disable feature
    2. Verify pw changed (no rule enforcing on new pws) or issue is seen
    3. Enable feature
    """
    pwh = system.security.password_hardening
    with allure.step("Disable feature"):
        pwh.set(PwhConsts.STATE, PwhConsts.DISABLED, apply=True).verify_result()
    with allure.step("Verify pwh configuration in show"):
        output = engines.dut.run_cmd("echo 'root:NvidiaR0ots!' | sudo chpasswd")
        logging.info("cmd output: {}".format(output))
        if output:
            assert False, "error: password not changed"
        else:
            logging.info("password was changed successfully")
    with allure.step("Enable feature"):
        pwh.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()
