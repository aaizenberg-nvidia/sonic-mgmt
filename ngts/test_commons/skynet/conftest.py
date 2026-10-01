'''
@summary:
    Conftest file
'''

# pylint MESSAGES CONTROL settings for this file:
# pylint: disable=redefined-outer-name

import ast
import logging
import pytest

from devts.infra.tools.topology_tools.topology_setup_utils import get_topology_by_setup_name

logger = logging.getLogger(__name__)


def pytest_addoption(parser):
    """
    Parse pytest options
    :param parser: pytest build-in
    """
    logger.info('Parsing pytest options')
    parser.addoption('--setup_name', action='store', required=True, default=None,
                     help='Setup name, example: sonic_tigris_r-tigris-06')
    parser.addoption('--expected_cpu_usage_dict', action='store', required=False, default=None, type=ast.literal_eval,
                     help='''Provide a dictionary with expected cpu thresholds.
                     Example:  --expected_ram_usage_dict='{"total":"20","sx_sdk":"10","syncd":"5","redis-server":"5",
                                                           "snmpd":"2","zebra":"2","bgpd":"2","bgpcfgd":"2",
                                                           "bgpmon":"2","fpmsyncd":"2","orchagent":"2","ntpd":"2",
                                                           "neighsyncd":"2","vlanmgrd":"2","intfmgrd":"2",
                                                           "portmgrd":"2","buffermgrd":"2","vrfmgrd":"2","nbrmgrd":"2",
                                                           "vxlanmgrd":"2","sensord":"2"}'
                                                           ''')
    parser.addoption('--expected_ram_usage_dict', action='store', required=False, default=None, type=ast.literal_eval,
                     help='''Provide a dictionary with expected ram thresholds.
                     Example:  --expected_ram_usage_dict='{"total":"4250","sx_sdk":"1000","syncd":"300",
                                                           "redis-server":"70","snmpd":"20","zebra":"85",
                                                           "bgpd":"75","bgpcfgd":"30","bgpmon":"20","fpmsyncd":"5",
                                                           "orchagent":"45","ntpd":"5","neighsyncd":"5",
                                                           "vlanmgrd":"5","intfmgrd":"5","portmgrd":"5",
                                                           "buffermgrd":"5","vrfmgrd":"5","nbrmgrd":"5",
                                                           "vxlanmgrd":"5","sensord":"10"}'
                                                           ''')
    parser.addoption('--partitions_and_expected_usage', action='store', required=False, default=None,
                     type=ast.literal_eval, help='''Provide a list of dictionary with expected partition usage.
                     Example:  --partitions_and_expected_usage='[{'partition': '/', 'max_usage': 9000},
                                                                 {'partition': '/var/log/', 'max_usage': 500}]'
                                                                 ''')
    parser.addoption('--nos_name', action='store', required=False, default=None,
                     help='Name of the Operation system')


@pytest.fixture(scope='session')
def setup_name(request):
    """
    Method for get setup name from pytest arguments
    :param request: pytest build-in
    :return: setup name
    """
    return request.config.getoption('--setup_name')


@pytest.fixture(scope='session')
def expected_cpu_usage_dict(request):
    """
    Method for get expected_cpu_usage_dict from pytest arguments
    :param request: pytest build-in
    :return: setup name
    """
    return request.config.getoption('--expected_cpu_usage_dict')


@pytest.fixture(scope='session')
def expected_ram_usage_dict(request):
    """
    Method for get expected_ram_usage_dict from pytest arguments
    :param request: pytest build-in
    :return: setup name
    """
    return request.config.getoption('--expected_ram_usage_dict')


@pytest.fixture(scope='session')
def partitions_and_expected_usage(request):
    """
    Method for get partitions_and_expected_usage from pytest arguments
    :param request: pytest build-in
    :return: setup name
    """
    return request.config.getoption('--partitions_and_expected_usage')


@pytest.fixture(scope='session', autouse=True)
def topology_obj(setup_name):
    """
    Fixture which create topology object before run tests and doing cleanup for ssh engines after test executed
    :param setup_name: example: sonic_tigris_r-tigris-06
    :return: topology object
    """
    return get_topology_by_setup_name(setup_name, slow_cli='')


@pytest.fixture(scope='session')
def nos_name(request):
    """
    Method for get NOS name from pytest arguments
    :param request: pytest build-in
    :return: nos_name
    """
    return request.config.getoption('--nos_name')
