import os
import re
import logging

from ngts.tools.infra import update_sys_path_by_community_plugins_path
from ngts.scripts.sonic_deploy.community_only_methods import is_dualtor_topo
from ngts.tools.test_utils.nvos_general_utils import get_switch_type
from ngts.nvos_constants.constants_nvos import TopologyConsts
from ngts.tools.infra import get_topology_from_noga

update_sys_path_by_community_plugins_path()

import plugins.conditional_mark as _community_conditional_mark  # noqa: E402
from plugins.conditional_mark import pytest_addoption, pytest_collection, pytest_collection_modifyitems  # noqa: E402

# Community strips a leading "tests/" before matching. NGTS YAML keys keep that
# prefix (tests/nightly/...), so retry with it when the stripped nodeid misses.
_find_all_matches = _community_conditional_mark.find_all_matches


def find_all_matches(nodeid, *args, **kwargs):
    matches = _find_all_matches(nodeid, *args, **kwargs)
    if not matches and not nodeid.startswith("tests/"):
        matches = _find_all_matches("tests/" + nodeid, *args, **kwargs)
    return matches


_community_conditional_mark.find_all_matches = find_all_matches


def testbed_param_already_loaded(session):
    return hasattr(session.config.option, 'testbed')


def get_setup_topology(session):
    setup_topology = 'ptf-any'
    if hasattr(session.config.option, 'sonic_topo'):
        if session.config.option.sonic_topo:
            setup_topology = session.config.option.sonic_topo
    return setup_topology


def get_list_of_ignore_condition_files(session, req_sub_string=""):
    conditions_file_regexp = r'tests_mark_conditions.*' + req_sub_string + r'.*.yaml\Z'
    relative_path = 'tests/common/plugins/conditional_mark/'
    conditions_folder_path = session.config.option.ansible_inventory.replace('ansible/inventory', relative_path)
    condition_files_list = []
    for file_name in os.listdir(conditions_folder_path):
        if re.match(conditions_file_regexp, file_name, re.IGNORECASE):
            condition_files_list.append(os.path.join(conditions_folder_path, file_name))
            logging.info(f"using condition file: {file_name}")
    return condition_files_list


def mark_conditions_files_param_already_provided(session):
    if session.config.option.mark_conditions_files:
        return True
    return False


def pytest_sessionstart(session):
    # In collect-only mode, skip expensive topology operations
    is_collect_only = session.config.getoption('--collect-only', default=False)

    setup_topology = get_setup_topology(session)
    condition_file_req_sub_string_name = ""

    # Only connect to topology if NOT in collect-only mode
    if not is_collect_only and not testbed_param_already_loaded(session):
        topology = get_topology_from_noga(session)
        dut_name = topology.players['dut']['attributes'].noga_query_data['attributes']['Common']['Name']
        session.config.option.testbed = f'{dut_name}-{setup_topology}'
        condition_file_req_sub_string_name = get_condition_file_req_sub_string_name(topology)

    if is_dualtor_topo(setup_topology):
        session.config.option.testbed = f'{session.config.option.setup_name}-{setup_topology}'

    testbed_file_full_path = session.config.option.ansible_inventory.replace('inventory', 'testbed.yaml')
    session.config.option.testbed_file = testbed_file_full_path

    if not mark_conditions_files_param_already_provided(session):
        condition_files_list = get_list_of_ignore_condition_files(session,
                                                                  req_sub_string=condition_file_req_sub_string_name)
        session.config.option.mark_conditions_files = condition_files_list


def get_condition_file_req_sub_string_name(topology):
    switch_type = get_switch_type(topology)
    if switch_type == TopologyConsts.SONIC:
        return ""
    return f"_{switch_type}_"
