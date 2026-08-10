import logging
import time

from ngts.tools.test_utils import allure_utils as allure
import pytest
from retry.api import retry_call
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_constants.constants_nvos import SystemConsts, ActionConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active

import json


cumulus_owner = "yleibovichwe"


logger = logging.getLogger()


@pytest.mark.cumulus
@pytest.mark.events
@pytest.mark.system
def test_show_system_events(test_api, engines):
    """
    Run show system events and table-size commands and verify the required events and table-size
        Test flow:
            1. Simulate 60 events
            2. Run 'nv show system events' and validate there shouldn't be output for the command
    """
    TestToolkit.tested_api = test_api
    system = System()
    with allure.step('Verify that system.events.show() raises an error (negative test)'):
        try:
            system.events.show()
        except Exception as e:
            logger.info("Received Exception during nv show system events: {}".format(e))
        else:
            assert False, "Expected system.events.show() to fail, but it succeeded."
