import pytest
from ngts.tools.test_utils import allure_utils as allure
import re
import logging
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.ConfigTool import ConfigTool
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_constants.constants_nvos import SystemConsts, ConfigConsts
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.tests_nvos.general.config_commands.helpers import *


cumulus_owner = "gosaini"


logger = logging.getLogger()


@pytest.mark.cumulus
@pytest.mark.general
@pytest.mark.configuration
@pytest.mark.simx
def test_config_delete_positive_flow(engines):
    """
    in this case we validate all possible ways to delete a revision,
    we can delete four types of revisions: 1. pending 2. detached 3. previous 4. invalid

    Test Flow:
    - deleting pending revision
        - Run nv set system message pre-login “TESTING”						- <rev_id>
        - Run nv config delete <rev_id> 									- Deleted <rev_id>
        - Run nv config attach <rev_id>										- Revision <rev_id> does not exist
        - Run nv config revision
    - deleting detached revision
        - Run nv set system message pre-login “TESTING”						- <rev_id>
        - Run nv config detach 												- detached <rev_id>
        - Run nv config revision											- Validate <rev_id> state is detached
        - Run nv config delete <rev_id>										- Deleted <rev_id>
        - Run nv config attach <rev_id>										- Revision <rev_id> does not exist
        - Run nv config revision											- Validate <rev_id> does not exist
    - deleting previous  revision
        - Run nv set system message pre-login “TESTING_PREV” + Apply		- Applied <rev_id_1>
        - Run nv set system message pre-login “TESTING” + Apply	Applied 	- <rev_id_2>
        - Run nv config delete <rev_id_1>									- Deleted <rev_id>
        - Run nv config attach <rev_id_1>									- Revision <rev_id> does not exist
        - Run nv config revision <rev_id_1>									- Validate <rev_id> does not exist
    :param engines:
    :return:
    """
    with allure.step("Create System"):
        system = System()

    with (allure.step("Testing nv config delete")):
        with allure.independent_step("delete from pending list"):
            with allure.step("Configure, but don't apply"):
                result_obj = system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE,
                                                op_param_value="Testing_pending")
                rev_id = get_revision_id(result_obj.returned_value, RevisionStatus.CREATED)

            with allure.step("Delete revision"):
                deleted_rev_id = get_revision_id(NvueGeneralCli.delete_config(engines.dut, rev_id),
                                                 RevisionStatus.DELETED)
                assert deleted_rev_id == rev_id, f"the deleted revision id is {deleted_rev_id}, the expected is {rev_id}"

        with allure.independent_step("delete a detached revision"):
            with allure.step("Configure, but don't apply"):
                rev_id = get_revision_id(system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE,
                                                            op_param_value="Testing_detached").returned_value)

            with allure.step("Detach revision"):
                detached_rev_id = get_revision_id(NvueGeneralCli.detach_config(engines.dut), RevisionStatus.DETACHED)
                assert detached_rev_id == rev_id, f"the detached revision id {detached_rev_id}, the expected is {rev_id}"

            with allure.step("Delete revision"):
                deleted_rev_id = get_revision_id(NvueGeneralCli.delete_config(engines.dut, detached_rev_id),
                                                 RevisionStatus.DELETED)
                assert deleted_rev_id == detached_rev_id, f"the deleted revision id is {deleted_rev_id}, the expected is {detached_rev_id}"

        with allure.independent_step("delete a previous revision"):
            with allure.step("Configure and apply"):
                result_obj = system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value="Testing_previous", apply=True)
                prev_rev_id = get_revision_id(result_obj.returned_value, RevisionStatus.APPLIED)

            with allure.step("Configure and apply"):
                result_obj = system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value="Testing", apply=True)
                rev_id = get_revision_id(result_obj.returned_value)
                assert prev_rev_id + 1 == rev_id, f"the applied revision id is {rev_id}, the expected is {prev_rev_id + 1}"

            with allure.step("Delete revision"):
                deleted_rev_id = get_revision_id(NvueGeneralCli.delete_config(engines.dut, prev_rev_id), RevisionStatus.DELETED)
                assert deleted_rev_id == prev_rev_id, f"the deleted revision id is {deleted_rev_id}, the expected is {prev_rev_id}"


@pytest.mark.cumulus
@pytest.mark.general
@pytest.mark.configuration
@pytest.mark.simx
def test_config_delete_negative_flow(engines):
    """
        Test flow:
        - Run nv set system message pre-login “TESTING” + Apply				- Applied <rev_id>
        - Run nv config revision											- Validate <rev_id> state is applied
        - Run nv config delete <rev_id>										- Error: Revision <rev_id> is currently applied and cannot be deleted
        - Run nv config delete startup										- Error: Revision startup is reserved and cannot be deleted
        - Run nv config delete <invalid_id>									- Revision <rev_id> does not exist
        """
    with allure.step("Create System"):
        system = System()

    with (allure.step("Testing nv config delete - Bad Flow")):
        with allure.independent_step("delete applied revision - Should Fail"):
            with allure.step("Configure and apply"):
                result_obj = system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value="Testing", apply=True)
                rev_id = get_revision_id(result_obj.returned_value)

            with allure.step("Delete revision"):
                error_msg = NvueGeneralCli.delete_config(engines.dut, rev_id)
                assert f"Error: Revision '{rev_id}' is currently applied and cannot be deleted" == error_msg, f"the error message is not as expected, {error_msg}"

        with allure.independent_step("delete startup revision"):
            with allure.step("Delete revision"):
                error_msg = NvueGeneralCli.delete_config(engines.dut, "startup")
                assert "Error: Revision startup is reserved and cannot be deleted" == error_msg, f"the error message is not as expected, {error_msg}"

        with allure.independent_step("delete startup revision"):
            with allure.step("Delete revision"):
                error_msg = NvueGeneralCli.delete_config(engines.dut, "-21")
                assert "Error: Revision -21 does not exist" == error_msg, f"the error message is not as expected, {error_msg}"


@pytest.mark.cumulus
@pytest.mark.general
@pytest.mark.configuration
@pytest.mark.simx
def test_config_attach_positive_flow(engines):
    """
    in this case we validate all possible ways to attach a revision,
    we can attach two types of revisions: 1. detached 3. previous 4. invalid

    Test Flow:
        - Run nv set system message pre-login “TESTING_DETACHED”			- <rev_id_1>
        - Run nv config detach 	detached <rev_id_1>
        - Run nv set system message pre-login “TESTING” + APPLY				- Applied <rev_id_2>
        - Run nv config attach <rev_id_1>									- attached <rev_id_1>
        - Run nv config revision											- Validate <rev_id_1> state is pending
        - Run nv config apply												- Applied <rev_id_1>
        - Run nv config revision											- Validate <rev_id_2> state is previous
        - Run nv config revision											- Validate <rev_id_1> state is pending
        - Run nv config apply												- Applied <rev_id_1>
        - Run nv config revision											- Validate <rev_id_2> state is previous
        - Run nv config attach <rev_id_2>									- attached <rev_id_2>
        - Run nv config apply												- Applied <rev_id_2>
        - Run nv show system message										- Pre-login = TESTING
        """
    with allure.step("Create System"):
        system = System()

    with allure.step("Testing nv config attach"):
        with allure.step("Configure, but don't apply"):
            result_obj = system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value="Testing_detached")
            rev_id = get_revision_id(result_obj.returned_value, RevisionStatus.CREATED)

        with allure.step("Detach revision"):
            detached_rev_id = get_revision_id(NvueGeneralCli.detach_config(engines.dut), RevisionStatus.DETACHED)
            assert detached_rev_id == rev_id, f"the detached revision id is {detached_rev_id}, the expected is {rev_id}"

        with allure.step("Configure and apply"):
            result_obj = system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value="Testing", apply=True)
            rev_id = get_revision_id(result_obj.returned_value, RevisionStatus.APPLIED)

        with allure.step("Attach detached revision"):
            with allure.step("Attach detached revision"):
                attach_output = NvueGeneralCli.attach_config(engines.dut, detached_rev_id)
                attached_rev_id = get_revision_id(attach_output, RevisionStatus.ATTACHED)
                logger.info(attached_rev_id)

            with allure.step("apply reattached revision"):
                get_revision_id(NvueGeneralCli.apply_config(engine=engines.dut, option='-y'), RevisionStatus.APPLIED)

        with allure.step("Attach previous revision"):
            attach_output = NvueGeneralCli.attach_config(engines.dut, rev_id)
            attached_rev_id = get_revision_id(attach_output, RevisionStatus.ATTACHED)
            logger.info(attached_rev_id)

            with allure.step("apply reattached revision"):
                get_revision_id(NvueGeneralCli.apply_config(engine=engines.dut, option='-y'),
                                RevisionStatus.APPLIED)


@pytest.mark.cumulus
@pytest.mark.general
@pytest.mark.configuration
@pytest.mark.simx
def test_config_attach_negative_flow(engines):
    """
    Test Flow:
    – bad flow, can’t attach invalid revision id
        - Run nv config attach <invalid_id>									- Revision <invalid_id> does not exist
        """
    with allure.step("attach invalid revision - Should Fail"):
        with allure.step(f"Attach revision -21"):
            error_msg = NvueGeneralCli.attach_config(engines.dut, "-21")
            assert "Revision -21 does not exist" == error_msg, f"the error message is not as expected, {error_msg}"


def get_revision_id(output, expected_pattern=""):
    """
    :param output:
    :param expected_pattern: can be applied, deleted, detached or attached
    :return:
    """
    if expected_pattern and expected_pattern not in output:
        return None
    match = re.search(r'rev_id: (\d+)', output)
    if match:
        return int(match.group(1))
    else:
        return None
