"""

conftest.py

Defines the methods and fixtures which will be used by pytest,
NOTE: Add here only fixtures and methods that can be used for canonical and community setups alike.

if your methods only apply for canonical setups please add them in ngts/tests/conftest.py

"""
import json
import logging
import os
import re
import random

import allure
import pytest
from dotted_dict import DottedDict
from paramiko.ssh_exception import SSHException

from infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.cli_wrappers.dvs.dvs_cli import DvsCli
from ngts.cli_wrappers.linux.linux_cli import LinuxCli, LinuxCliStub
from ngts.cli_wrappers.nvue.nvue_cli import NvueCli
from ngts.cli_wrappers.sonic.sonic_cli import SonicCli, SonicCliStub
from ngts.cli_wrappers.sonic.sonic_general_clis import SonicGeneralCliDefault
from ngts.constants.constants import InfraConst, PytestConst, NvosCliTypes, DebugKernelConsts, CliType, SonicConst
from ngts.constants.constants import SerialLoggerConst
from ngts.helpers.general_helper import get_all_setups, get_dut_cli_obj_from_topo_obj
from ngts.helpers.sonic_branch_helper import get_sonic_branch, update_branch_in_topology, update_sanitizer_in_topology, \
    get_sonic_image
from ngts.nvos_tools.infra.RegressionConfigurations import Configurations
from ngts.nvos_tools.ib.opensm.OpenSmTool import OpenSmTool, DOCA_OPEN_SM_PATH, SM_MASTER_OPEN_SM_PATH
from ngts.tests.nightly.app_extension.app_extension_helper import APP_INFO, get_app_repository
from ngts.tools.allure_report.allure_report_attacher import add_fixture_end_tag, add_fixture_name, \
    clean_stored_cmds_with_fixture_scope, update_fixture_scope_list, enable_record_cmds
from ngts.tools.infra import get_platform_info, get_devinfo, is_deploy_run, get_chip_type
from ngts.tools.infra import get_topology_from_noga
from ngts.tools.test_utils.nvos_general_utils import get_switch_type
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.scripts.allure_summary.bug_marker import apply_bug_markers, finalize_bug_categories
from ngts.scripts.sonic_deploy.sonic_only_methods import detect_asic_count, SonicInstallationSteps

from ngts.ngts_types import CleanUpT

logger = logging.getLogger(__name__)


def _disable_logging_root_handlers():
    """
    Disable logging root handlers if pytest is running in live mode (-s / --capture=no).
    """
    root, has_live_hdlr = logging.getLogger(), False
    stream_hdlrs: list[logging.StreamHandler] = []

    from _pytest.logging import _LiveLoggingStreamHandler

    for hdlr in root.handlers:
        if isinstance(hdlr, _LiveLoggingStreamHandler):  # pytest live logging handler
            has_live_hdlr = True
        elif isinstance(hdlr, logging.StreamHandler):  # default python root logging stream handler
            stream_hdlrs.append(hdlr)

    if has_live_hdlr:
        for hdlr in stream_hdlrs:
            hdlr.setLevel(logging.ERROR)


def pytest_sessionstart(session: pytest.Session):
    """Clear cached variables from previous pytest session"""

    session.config.cache.set(PytestConst.LA_DYNAMIC_IGNORES_LIST, None)
    session.config.cache.set(PytestConst.CUSTOM_SKIP_IF_DICT, None)
    session.config.cache.set(PytestConst.CUSTOM_TEST_SKIP_PLATFORM_TYPE, None)
    session.config.cache.set(PytestConst.CUSTOM_TEST_SKIP_BRANCH_NAME, None)
    session.config.cache.set(PytestConst.CUSTOM_TEST_SKIP_IMAGE_TYPE, None)

    # Disable stream logging if pytest is running in live mode
    _disable_logging_root_handlers()


def pytest_collection(session: pytest.Session):
    # If pytest is running in collect-only mode, skip the rest of the code
    # WHY? because we want the collection mode to run faster
    if session.config.getoption('--collect-only'):
        return

    topology = get_topology_from_noga(session)
    logger.debug('Get switch devdescription from Noga')
    switch_attributes = topology.players['dut']['attributes'].noga_query_data['attributes']
    devinfo = get_devinfo(switch_attributes)

    platform = json.loads(devinfo).get('platform')
    session.config.cache.set(PytestConst.CUSTOM_TEST_SKIP_PLATFORM_TYPE, platform)
    if is_deploy_run():
        # Required for prevent SSH attempts into DUT at the beginning of deploy image test(in case when device in ONIE)
        branch = 'master'
        image = ''
        session.config.cache.set(PytestConst.IS_SANITIZER_IMAGE, False)
    else:
        branch = get_sonic_branch(topology)
        image = get_sonic_image(topology)

    session.config.cache.set(PytestConst.CUSTOM_TEST_SKIP_BRANCH_NAME, branch)
    session.config.cache.set(PytestConst.CUSTOM_TEST_SKIP_IMAGE_TYPE, image)


def pytest_collection_modifyitems(config, items):
    # Promote @pytest.mark.bug(redmine=...) into native Allure issue links and
    # a "known_bug" tag, so the report shows a clickable Redmine link on each
    # failing test and the tag is filterable in the Allure UI - same UI
    # control as the existing "flaky" chip.
    apply_bug_markers(items)


def pytest_sessionfinish(session, exitstatus):
    # After all tests have written their result JSONs, stamp a sentinel into
    # failed-test messages and emit categories.json so the Allure dashboard
    # gets a "Failures caused by open bugs" bucket alongside flaky/new-failed.
    alluredir = getattr(session.config.option, "allure_report_dir", None)
    if alluredir:
        # Pass setup_name so setup_filters on platform-specific bugs are
        # honored. Without it, a taipan-only bug whose error_pattern hits
        # a crocodile failure's statusMessage would attach incorrectly.
        try:
            setup_name = session.config.getoption("--setup_name", default=None)
        except Exception:
            setup_name = None
        finalize_bug_categories(alluredir, setup_name=setup_name)


pytest_plugins = (
    'ngts.tools.sysdumps',
    'ngts.tools.conditional_mark',
    'ngts.tools.loganalyzer',
    'ngts.tools.serial_logger.analyzer',
    'ngts.tools.infra',
    'pytester',
    'ngts.tools.allure_report',
    'ngts.tools.mars_test_cases_results',
    'ngts.tools.loganalyzer_dynamic_errors_ignore.la_dynamic_errors_ignore',
    'tests.common.plugins.collect_test_data_to_sql',
    'ngts.tools.ports_modifier',
    'tests.common.plugins.random_seed',
    'tests.common.plugins.cli_coverage',
)


def pytest_addoption(parser):
    """
    Parse pytest options
    :param parser: pytest builtin
    """
    logger.info('Parsing pytest options')
    parser.addoption('--setup_name', action='store', required=True, default=None,
                     help='Setup name, example: sonic_tigris_r-tigris-06')
    parser.addoption('--dut_hwsku', action='store', required=False, default=None, help='DUT hwsku')
    parser.addoption('--base_version', action='store', default=None, help='Path to base SONiC version')
    parser.addoption('--downgrade_version', action='store', default=None, help='Path to downgrade SONiC version')
    parser.addoption('--issu_version', action='store', default=None, help='Path to issu SONiC version')
    parser.addoption('--target_version', action='store', default=None, help='Path to target SONiC version')
    parser.addoption('--ansible_inventory_file', action='store', default='False', help='Path to ansible inventory file - for playbooks')
    parser.addoption('--fw_versions_json_file', action='store', default=None, help='Path to component_versions json file')
    default_fw_pkg = os.path.join(
        os.path.dirname(__file__),
        '../tests/platform_tests/fwutil/firmware.json'
    )
    parser.addoption('--fw-pkg', action='store', default=default_fw_pkg,
                     help='Path to firmware package (tar.gz or json file) for BMC/CPLD updates')
    parser.addoption('--wjh_deb_url', action='store', default=None, help='URL path to WJH deb package')
    parser.addoption("--session_id", action="store", default=None, help="Number of mars session id.")
    parser.addoption("--mars_key_id", action="store", default=None, help="mars key id.")
    parser.addoption("--test_name", action="store", default=None,
                     help="a parameter for script check_and_store_sanitizer_dump.py, "
                          "will check for sanitizer failures and store dump under test name")
    parser.addoption("--send_mail", action="store", default=False,
                     help="a boolean parameter for script check_and_store_sanitizer_dump.py, "
                          "will send mail with the sanitizer failures")
    parser.addoption("--skynet", action="store_true", default=False,
                     help="a boolean parameter for identify skynet tests")
    parser.addoption("--tech_support_duration", action="store", default=None, help="duration of tech support for test")
    parser.addoption(PytestConst.run_config_only_arg, action='store_true', help='If set then only the configuration '
                                                                                'part defined in the push_build '
                                                                                'conftest will be executed')
    parser.addoption(PytestConst.run_test_only_arg, action='store_true', help='If set then only the test(push_build) '
                                                                              'will be executed')
    parser.addoption(PytestConst.run_cleanup_only_arg, action='store_true', help='If set then only the cleanup part '
                                                                                 'defined in the push_build conftest '
                                                                                 'will be executed')
    parser.addoption('--app_extension_dict_path', action='store', required=False, default=None,
                     help='''Provide path to application extensions json file.
                          'Example of content: {"p4-sampling":"nbu-harbor.gtm.nvidia.com/sonic-p4/p4-sampling:0.2.0",
                                      "what-just-happened":"nbu-harbor.gtm.nvidia.com/sonic-wjh/docker-wjh:1.0.1"} ''')
    parser.addoption("--nvos_api_type", action="store", default='nvue', help="nvue/openapi")
    parser.addoption("--current_topo", dest="current_topo",
                     help="Current topology for example: t0, t1, t1-lag, ptf32, ...")
    parser.addoption("--expected_topo", dest="expected_topo",
                     help="Expected topology, for example: t0, t1, t1-lag, ptf32, ...")
    parser.addoption("--skip_auto_checks", action="store_true", default=False, required=False,
                     help="Whether to skip 'is debug-kernel/coverage/sanitizer' auto checks for the session, or not")
    parser.addoption("--sonic-topo", action="store",
                     help="The topo for SONiC testing, for example: t0, t1, t1-lag, ptf32, ...")
    parser.addoption('--default_pass_env_var', action='store', default='',
                     help='Which environment variable to use for default dut password')
    parser.addoption("--skip_bug_handler_action", action="store_true", default=False, required=False,
                     help="Whether to skip (True) log analyzer bug handler actions when loganalyzer is enabled, or not (False)")
    parser.addoption('--fail_install_if_secure_boot_off', action='store', default='yes',
                     help='Whether to fail NVOS installation due to disabled Secure-Boot')
    parser.addoption('--skip_weekend_cases', required=False, action='store', default="",
                     help='Parameter used to decide which kind of test case to run.'
                          'when it is "yes", it will only run "Daily" test case, '
                          'when it is "no", it will only run weekend test cases, '
                          'when it is other value, it will run both daily and weekend test cases,'
                          'user need to define which is daily and weekend test case')
    parser.addoption('--target_cli_type', dest="target_cli_type", required=False, action='store',
                     default=False,
                     help='On Performance deploy, this parameter will override the cli type defined in Noga,'
                          ' and will set the relevant cli')
    parser.addoption('--is_ipv6', dest="is_ipv6", required=False, action="store_true", default=False,
                     help='On Performance tests, this parameter will determine if the tests run with ipv4 or ipv6')
    parser.addoption('--parameter_file_location', dest="parameter_file_location", required=False, action='store', default=None,
                     help='location of the parameter file for ar parameters optimiser')
    parser.addoption(SerialLoggerConst.CMD_LINE_KEY, action='store', required=False,
                     choices=SerialLoggerConst.CMD_LINE_VALUES, default=SerialLoggerConst.MODE_OFF,
                     help='Action for serial log handler. Options: off (no serial logging), store (without analyzing), '
                          'analyze, analyze_and_open_bugs')
    parser.addoption('--store_dump_on_fail', required=False, action='store_true', default=False,
                     help='Store techsupport dump on test fail during manual run')
    parser.addoption("--ipv6_add", action="store", default=None, help="Provide static ipv6 address")
    parser.addoption("--skip_coredump_check", action="store_true", default=False, help="Skip coredump check fixture")
    parser.addoption("--ib_router", action="store_true", default=False, help="a boolean parameter to force ib router configuration")


def pytest_runtest_call(item):
    """
    Pytest hook which is executed at the calling of test case
    :param item: pytest builtin
    """
    topology_obj = item.funcargs.get('topology_obj')
    if topology_obj:
        add_fixture_end_tag(topology_obj)


def pytest_fixture_setup(fixturedef, request):
    """
    Pytest hook which is executed at the beginning of each fixture
    :param fixturedef: pytest builtin
    :param request: pytest builtin
    """
    func_name = request._pyfuncitem.name
    for func in request.session.items:
        if func.name == func_name and getattr(func, 'funcargs', None):
            topology_obj = func.funcargs.get('topology_obj')
            if topology_obj:
                add_fixture_name(topology_obj, request.fixturename, fixturedef.scope)


def pytest_fixture_post_finalizer(fixturedef, request):
    """
    Pytest hook which is executed at the beginning of each fixture
    :param fixturedef: pytest builtin
    :param request: pytest builtin
    """
    func_name = request._pyfuncitem.name
    for func in request.session.items:
        if func.name == func_name and getattr(func, 'funcargs', None):
            topology_obj = func.funcargs.get('topology_obj')
            if topology_obj:
                if getattr(func, 'rep_setup', None) and getattr(func, 'rep_call',
                                                                None) and func.rep_setup.passed and func.rep_call.failed:
                    update_fixture_scope_list(topology_obj, fixturedef.argname, fixturedef.scope)
                else:
                    clean_stored_cmds_with_fixture_scope(topology_obj, fixturedef.argname, fixturedef.scope)


@pytest.fixture(scope="package")
def app_extension_dict_path(request):
    """
    Method for getting base version from pytest arguments
    :param request: pytest builtin
    :return: app_extension_dict
    """
    return request.config.getoption('--app_extension_dict_path')


@pytest.fixture(scope="session")
def verify_secure_boot(request):
    """
    For NVOS, understand whether the user wants not to fail installation if Secure-Boot is disabled.
    user can skip this check only if this execution line option is given with value 'no'/'false' (no case sensitivity)

    :return: True if should fail installation if Secure-Boot is disabled; False, otherwise (when user specifies it)
    """
    value = request.config.getoption('--fail_install_if_secure_boot_off')
    value = value.lower() if value and isinstance(value, str) else ''
    return value not in ['no', 'false']


@pytest.fixture(scope="session")
def base_version(request):
    """
    Method for getting base version from pytest arguments
    :param request: pytest builtin
    :return: base_version argument value
    """
    return request.config.getoption('--base_version')


@pytest.fixture(scope="session")
def downgrade_version(request):
    """
    Method for getting base version from pytest arguments
    :param request: pytest builtin
    :return: downgrade_version argument value
    """
    return request.config.getoption('--downgrade_version')


@pytest.fixture(scope="session")
def is_ipv6(request):
    """
    Method for getting is_ipv6 from pytest arguments
    :param request: pytest builtin
    :return: is_ipv6 argument value
    """
    return request.config.getoption('--is_ipv6')


@pytest.fixture(scope="session")
def issu_version(request):
    """
    Method for getting base version from pytest arguments
    :param request: pytest builtin
    :return: issu_version argument value
    """
    return request.config.getoption('--issu_version')


@pytest.fixture(scope="session")
def target_version(request):
    """
    Method for getting target version from pytest arguments
    :param request: pytest builtin
    :return: target_version argument value
    """
    target_version = request.config.getoption('--target_version')
    cli_type = request.config.getoption('--target_cli_type')
    if cli_type == 'DVS':
        target_version = parse_dvs_target_version(request)
    return target_version


def parse_dvs_target_version(request):
    sdk_pointer = request.config.getoption('--target_version')
    dvs_target_version = os.popen(f'cat {sdk_pointer}/SDK_VERSION').read().replace('\n', '')
    return f"sx_sdk_eth-{dvs_target_version}"


@pytest.fixture(scope="session")
def wjh_deb_url(request):
    """
    Method for getting what-just-happened deb file URL from pytest arguments
    :param request: pytest builtin
    :return: wjh_deb_url argument value
    """
    return request.config.getoption('--wjh_deb_url')


@pytest.fixture(scope='session')
def setup_name(request):
    """
    Method for get setup name from pytest arguments
    :param request: pytest builtin
    :return: setup name
    """
    return request.config.getoption('--setup_name')


@pytest.fixture(scope='session')
def ansible_inventory_file(request):
    """
    Method for get ansible_inventory_file from pytest arguments
    :param request: pytest builtin
    :return: setup name
    """
    value = request.config.getoption('--ansible_inventory_file')
    if not value:
        value = 'False'
    return value


@pytest.fixture(scope='session')
def fw_versions_json_file(request):
    """
    Method for get fw versions json file from pytest arguments - example:
    "fw_versions_json_file": "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/juliet_versions_2200.json"
    :param request: pytest builtin
    :return: file path
    """
    return request.config.getoption('--fw_versions_json_file')


@pytest.fixture(scope='module')
def fw_pkg(request):
    """
    Fixture to extract firmware data from package file.
    """
    from tests.common.helpers.firmware_helper import extract_fw_data

    fw_pkg_path = request.config.getoption('--fw-pkg')
    if not fw_pkg_path or not os.path.exists(fw_pkg_path):
        pytest.skip("No valid fw package specified.")
    return extract_fw_data(fw_pkg_path)


@pytest.fixture(scope='session')
def has_loopbox(topology_obj):
    """
    Method to check if system has loopbox.
    :param topology_obj: the setup topology object
    :return: if setup has loopbox or not
    """
    with allure.step('Checking if system has loopbox'):
        dut_setup_common_attributes = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Common']
        labels = dut_setup_common_attributes.get('labels')
        return labels and InfraConst.LOOPBOX_LABEL in labels


@pytest.fixture(scope='session')
def standalone_system(setup_name):
    """
    Method to check if system has loopbox.
    :param setup_name: the setup name
    :return: if setup has loopbox or not
    """
    return setup_name not in Configurations.non_standalone_systems


@pytest.fixture(scope='session')
def doca_traffic_system(setup_name):
    """
    Method to check if system is a doca traffic system.
    :param setup_name: the setup name
    :return: if setup is a doca traffic system or not
    """
    return setup_name in Configurations.doca_traffic_systems


@pytest.fixture(scope='session', autouse=True)
def configure_opensm_path(doca_traffic_system):
    """
    Configure OpenSmTool.OPENSM_PATH based on whether this is a doca traffic system.
    This runs automatically at the beginning of the test session.
    """
    OpenSmTool.set_opensm_path(DOCA_OPEN_SM_PATH if doca_traffic_system else SM_MASTER_OPEN_SM_PATH)


@pytest.fixture(scope='session')
def wrong_shunt_resistor_system(setup_name):
    """
    Method to check if system has loopbox.
    :param setup_name: the setup name
    :return: if setup has loopbox or not
    """
    return setup_name in Configurations.systems_with_wrong_shunt_resistor


@pytest.fixture(scope="session")
def skip_weekend_cases(request):
    """
    Method for getting base version from pytest arguments
    :param request: pytest builtin
    :return: base_version argument value
    """
    return request.config.getoption('--skip_weekend_cases')


@pytest.fixture(scope='session')
def asic_count(initial_topology_obj, platform_params):
    dut_engine = initial_topology_obj.players['dut']['engine']
    return detect_asic_count(dut_engine, platform_params)


@pytest.fixture(scope='session')
def tested_asic_index(asic_count):
    tested_asic_index = SonicConst.DEFAULT_TESTED_ASIC_INDEX
    if asic_count > SonicConst.DEFAULT_ASIC_COUNT:
        tested_asic_index = random.randrange(asic_count)
    return tested_asic_index


@pytest.fixture(scope='session')
def is_multi_asic(platform_params):
    return SonicInstallationSteps.is_multi_asic_platform(platform_params=platform_params)


def set_asic_for_dut_clis(topology, tested_asic_index):
    """Set ASIC for dut CLI and stub CLI objects in topology (resets cached sub-CLIs)."""
    topology.players['dut']['cli'].set_asic(tested_asic_index)
    topology.players['dut']['stub_cli'].set_asic(tested_asic_index)


@pytest.fixture(scope='session', autouse=True)
def topology_obj(initial_topology_obj, tested_asic_index, is_multi_asic, is_ipv6):
    topo = initial_topology_obj
    if is_multi_asic:
        for side in ('ha', 'hb'):
            topo.players[side] = topo.players.pop(f'{side}-{tested_asic_index}')
            for i in (1, 2):
                topo.ports[f'{side}-dut-{i}'] = topo.ports.pop(f'{side}-{tested_asic_index}-dut-{i}')
                topo.ports[f'dut-{side}-{i}'] = topo.ports.pop(f'dut-{side}-{tested_asic_index}-{i}')
        set_asic_for_dut_clis(topo, tested_asic_index)
    return topo


@pytest.fixture(scope='session')
def initial_topology_obj(setup_name, request, is_performance, is_ipv6):
    """
    Fixture which create topology object before run tests and doing cleanup for ssh engines after test executed
    :param setup_name: example: sonic_tigris_r-tigris-06
    :param request: pytest builtin
    """
    with allure.step('Creating topology object'):
        cli_type = target_cli_type(request)
        topology = get_topology_from_noga(request.session, slow_cli=False, override_type=cli_type, force_update=True)

    with allure.step("Update branch in topology according to the current SONiC branch"):
        branch = request.session.config.cache.get(PytestConst.CUSTOM_TEST_SKIP_BRANCH_NAME, None)
        update_branch_in_topology(topology, branch)
        logger.info(f"branch: {branch}")

    with allure.step("Check if it's sanitizer image and update in topology"):
        is_sanitizer = request.session.config.cache.get(PytestConst.IS_SANITIZER_IMAGE, None)
        update_sanitizer_in_topology(topology, is_sanitizer=is_sanitizer)
        logger.info(f"is_sanitizer: {is_sanitizer}")

    with allure.step("Update CLI types in topology"):
        update_topology_with_cli_class(topology, request, is_performance)
        export_cli_type_to_cache(topology, request)
        enable_record_cmds(topology)

    if request.config.option.ports_number == "max":
        with allure.step("Update topology for fast reboot with max ports"):
            config_db = topology.players['dut']['cli'].general.get_config_db()
            topology.players_all_ports['dut'] = list(config_db['PORT'].keys())

    if is_ipv6 and 'sonic-mgmt' in topology.players:
        sonic_mgmt_engine = topology.players['sonic-mgmt']['engine']
        ipv6_addr = IpTool.get_routable_ipv6_from_any_interface(sonic_mgmt_engine)
        if ipv6_addr:
            sonic_mgmt_engine.switch_reachable_ip = ipv6_addr
            logger.info(f'Set sonic-mgmt switch_reachable_ip to {ipv6_addr}')
        else:
            logger.error(
                'IPv6 mode enabled but no routable IPv6 found on sonic-mgmt. '
                'SCP operations from switch will use IPv4 and likely fail.'
            )

    yield topology

    with allure.step('Cleaning-up the topology object'):
        for player_name, player_attributes in topology.players.items():
            if disconnect := getattr(player_attributes['engine'], 'disconnect', None):
                disconnect()


@pytest.fixture(scope='session')
def switch_type(topology_obj):
    return get_switch_type(topology_obj)


def target_cli_type(request):
    """
    Override cli type parameter in  Noga  to support different cli types on same switch
    """
    if request.config.getoption('--target_cli_type'):
        cli_type = request.config.getoption('--target_cli_type')
        return cli_type


def update_default_password(dut, request):
    default_password_env_var = request.config.getoption('--default_pass_env_var')
    if default_password_env_var:
        dut_password = os.getenv(default_password_env_var)
        assert dut_password is not None, 'Default environment password variable is not provided'
        dut['engine'] = LinuxSshEngine(dut['engine'].ip, dut['engine'].username,
                                       dut_password)


def update_topology_for_mlnxos_setups(topology, request):
    if 'sl_serial' in topology.players.keys():
        topology.players['dut_serial'] = topology.players.pop('sl_serial')
    if 'vm_player' in topology.players.keys():
        topology.players['server'] = topology.players.pop('vm_player')

    if request.config.getoption("--is_ipv6"):
        logger.info("IPv6 testing mode enabled")
        _update_ipv6_add(topology,
                         request.config.getoption("--ipv6_add", None))


def _update_ipv6_add(topology, ipv6_addr=None):
    """
    Update the IPv6 address for the given engine.
    """
    if not ipv6_addr:
        logger.info("No IPv6 address provided, attempting to get from DUT")
        ipv6_addr = IpTool.get_dut_ipv6_addr_of_given_eth_interface_using_nv_cli("eth0",
                                                                                 topology.players['dut']['engine'])

    if ipv6_addr:
        logger.info(f"Setting DUT IP to IPv6 address: {ipv6_addr}")
        topology.players['dut']['engine'].ip = ipv6_addr
    else:
        error_msg = "IPv6 testing enabled but no valid IPv6 address found for DUT"
        logger.error(error_msg)
        raise Exception(error_msg)


@pytest.fixture(scope='session')
def cli_objects(topology_obj):
    cli_obj_data = DottedDict()
    for player in topology_obj.players:
        cli_obj_data[player] = topology_obj.players[player]['cli']
    return cli_obj_data


def export_cli_type_to_cache(topology, request):
    """
    This function will cache set a variable called CLI_TYPE that indicates what is the Cli Type, NVUE Or Sonic.
    :param topology: topology object
    :param request: pytest builtin
    """
    cli_type = topology[0]['dut']['attributes'].noga_query_data['attributes']['Topology Conn.']['CLI_TYPE']
    request.session.config.cache.set('CLI_TYPE', cli_type)
    os.environ['CLI_TYPE'] = cli_type


def update_topology_with_cli_class(topology, request=None, is_performance=False):
    # TODO: determine player type by topology attribute, rather than alias
    nvos_setup = False
    for player_key, player_info in topology.players.items():
        cli_type = player_info['attributes'].noga_query_data['attributes']['Topology Conn.']['CLI_TYPE']
        if player_key == 'dut' or player_key == 'left_tg' or player_key == 'right_tg':
            if cli_type in NvosCliTypes.NvueCliTypes:
                update_nvos_topology(topology, player_key, player_info, request)
                nvos_setup = True
            elif cli_type == CliType.SONIC:
                player_info['cli'] = SonicCli(topology, dut_alias=player_key)
                player_info.update({'stub_cli': SonicCliStub(topology)})
            elif cli_type == CliType.DVS:
                player_info['cli'] = DvsCli(topology=topology, dut_alias=player_key)

        elif player_key == 'fanout' or player_key == 'fanout-b':
            if cli_type == CliType.SONIC:
                player_info['cli'] = SonicCli(topology, dut_alias=player_key)
            else:
                player_info['cli'] = LinuxCli(player_info['engine'])
                player_info.update({'stub_cli': LinuxCliStub(player_info['engine'])})

        elif player_key == 'dut-b':
            player_info['cli'] = SonicCli(topology, dut_alias=player_key)

        else:
            player_info['cli'] = LinuxCli(player_info['engine'])
            player_info.update({'stub_cli': LinuxCliStub(player_info['engine'])})

    if nvos_setup:
        if is_performance:
            return
        update_topology_for_mlnxos_setups(topology, request)  # for NVOS setups only


def update_nvos_topology(topology, player_key, player_info, request):
    if player_info['attributes'].noga_query_data['attributes']['Topology Conn.']['CLI_TYPE'] != "NVUE":
        player_info['engine'] = LinuxSshEngine(player_info['engine'].ip, player_info['engine'].username,
                                               player_info['engine'].password)
        player_info['attributes'].noga_query_data['attributes']['Topology Conn.']['CLI_TYPE'] = "NVUE"
        player_info['attributes'].noga_query_data['attributes']['Common']['Description'] = player_key
    player_info['cli'] = NvueCli(topology, player_info['engine'], dut_alias=player_key)
    player_info['is_nvos'] = True
    switch_type = player_info['attributes'].noga_query_data['attributes']['Specific'].get('TYPE', '')
    if switch_type == NvosCliTypes.CumulusCliType:
        player_info['is_cumulus'] = True
    update_default_password(player_info, request)


@pytest.fixture(scope='session')
def show_platform_summary(initial_topology_obj):
    return get_platform_info(initial_topology_obj)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    """
    Pytest hook which are executed in all phases: Setup, Call, Teardown
    :param item: pytest builtin
    :param call: pytest builtin
    """
    # Filter out unnecessary logs captured on "stderr"
    # allure will only attach log file to save disk space
    sections_to_remove = set(["stderr"])
    if item.name not in {"test_deploy_and_upgrade"}:
        # also remove stdout for tests that are not test_deploy_and_upgrade
        sections_to_remove.add("stdout")
    item._report_sections = list([report for report in item._report_sections if report[1] not in sections_to_remove])
    # execute all other hooks to obtain the report object
    outcome = yield
    rep = outcome.get_result()

    # set a report attribute for phase of a call, which can
    # be "setup", "call", "teardown"

    setattr(item, "rep_" + rep.when, rep)


@pytest.fixture(scope='session')
def dut_mac(engines, cli_objects):
    """
    Fixture which get DUT mac address from DUT config_db.json file
    :param engines: engines fixture
    :param cli_objects: cli_objects fixture
    :return: dut mac address
    """
    logger.info('Getting DUT mac address')
    config_db = cli_objects.dut.general.get_config_db()
    dut_mac = config_db.get('DEVICE_METADATA').get('localhost').get('mac')
    logger.info('DUT mac address is: {}'.format(dut_mac))
    return dut_mac


@pytest.fixture(scope='session')
def chip_type(topology_obj):
    switch_attributes = topology_obj.players['dut']['attributes'].noga_query_data['attributes']
    return get_chip_type(switch_attributes)


@pytest.fixture(scope='session')
def sonic_version(engines):
    """
    Pytest fixture which are returning current SONiC installed version
    :param engines: dictionary with available engines
    :return: string with current SONiC version
    """
    sonic_version_output = engines.dut.run_cmd('sudo sonic-cfggen -y /etc/sonic/sonic_version.yml -v build_version')
    sonic_ver = sonic_version_output.strip()
    return sonic_ver


@pytest.fixture(scope='session')
def sonic_branch(topology_obj):
    """
    Pytest fixture which are returning current SONiC branch which defined in the /etc/sonic/sonic_version.yml
    :param topology_obj: topology_obj fixture
    :return: the branch name
    """
    return get_sonic_branch(topology_obj)


@pytest.fixture(scope='session', autouse=True)
def should_skip_checking_fixture(request):
    if request.config.getoption("--skip_auto_checks"):
        logger.info('NVOS: Should skip auto test pre-checks')
        return True
    else:
        return False


@pytest.fixture(scope='session', autouse=True)
def is_sanitizer_image(topology_obj, should_skip_checking_fixture):
    pytest.is_sanitizer = False

    if should_skip_checking_fixture:
        logger.info('NVOS: Skip sanitizer run check')
        return pytest.is_sanitizer

    update_sanitizer_in_topology(topology_obj)
    pytest.is_sanitizer = topology_obj.players['dut']['sanitizer']
    return pytest.is_sanitizer


@pytest.fixture(scope='session', autouse=True)
def is_code_coverage_run(topology_obj, should_skip_checking_fixture):
    pytest.is_code_coverage = False

    if should_skip_checking_fixture:
        logger.info('NVOS: Skip coverage run check')
        return pytest.is_code_coverage

    try:
        pytest.is_code_coverage = bool(topology_obj.players['dut']['cli'].general.echo('${COVERAGE_FILE}'))
        logger.info(f'Code coverage image: {pytest.is_code_coverage}')
    except SSHException as err:
        logger.warning(f'Unable to check if its code coverage run. Assuming that the device is not reachable. '
                       f'Setting the is_code_coverage_run as False, '
                       f'Got error: {err}')
    return pytest.is_code_coverage


@pytest.fixture(scope='session', autouse=True)
def is_debug_kernel_run(engines, should_skip_checking_fixture):
    pytest.is_debug_kernel = False

    if should_skip_checking_fixture:
        logger.info('NVOS: Skip debug kernel run check')
        return pytest.is_debug_kernel

    try:
        output = engines.dut.run_cmd(f"sudo ls {DebugKernelConsts.KMEMLEAK_PATH}")
        pytest.is_debug_kernel = False if "No such file or directory" in output else True  # only in debug kernel version we have this file
    except SSHException as err:
        logger.warning(f'Unable to check if its debug kernel run. Assuming that the device is not reachable. '
                       f'Setting the is_debug_kernel_run as False, '
                       f'Got error: {err}')
    return pytest.is_debug_kernel


@pytest.fixture(scope='session', autouse=True)
def is_ci_run(setup_name):
    pytest.is_ci_run = "_CI_" in setup_name
    return pytest.is_ci_run


@pytest.fixture(scope="session", autouse=True)
def mars_key_id(request):
    return request.config.getoption("--mars_key_id")


@pytest.fixture(scope='session', autouse=True)
def is_mars_run(mars_key_id):
    pytest.is_mars_run = True if mars_key_id else False
    return pytest.is_mars_run


@pytest.fixture(scope='session', autouse=True)
def dynamic_ignore_set():
    pytest.dynamic_ignore_set = set()


@pytest.fixture(scope='session')
def platform_params(show_platform_summary, setup_name, initial_topology_obj):
    """
    Method for getting all platform related data
    :return: dictionary with platform data
    """
    platform_data = DottedDict()
    platform_data.platform = show_platform_summary['platform']
    platform_data.filtered_platform = re.search(
        r"(msn\d{4}a\w?|msn\d{4}c|msn\d{4}|sn\d{4}\_(?!simx)[a-z]+|sn\d{4}[a-z]?|qm\d{4}|q\d{4}|mqm\d{4}|mbf.*c|900.*a|bf.*dpu|N5110_LD|N5100_LD|N5112_LD|N5200_LD|N5600_LD|N5500_LD|N6150_LD|[Nn]6100_LD|[Nn]7170_LD)",
        show_platform_summary['platform'], re.IGNORECASE).group(1)
    if 'air' in setup_name.lower():
        platform_data.hwsku = json.loads(initial_topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']['devdescription'])['hwsku']
    else:
        platform_data.hwsku = show_platform_summary['hwsku']
    platform_data.setup_name = setup_name
    platform_data.asic_type = show_platform_summary["asic_type"]
    platform_data.asic_count = show_platform_summary["asic_count"]
    platform_data.host_name = initial_topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Common']['Name']
    return platform_data


@pytest.fixture(scope="session")
def upgrade_params(base_version, target_version, wjh_deb_url):
    """
    Method for getting all upgrade related parameters
    :return: dictionary with upgrade parameters
    """
    upgrade_data = DottedDict()

    upgrade_data.base_version = base_version
    upgrade_data.target_version = target_version
    upgrade_data.wjh_deb_url = wjh_deb_url
    upgrade_data.is_upgrade_required = False
    if base_version and target_version and base_version != target_version:
        upgrade_data.is_upgrade_required = True
    else:
        logger.info('Either one or all the upgrade arguments is missing, or base and target version are same, skipping the upgrade flow')
    return upgrade_data


@pytest.fixture(scope="session")
def shared_params(is_air):
    shared_dict = DottedDict()
    shared_dict.app_ext_is_app_ext_supported = False
    shared_dict.app_ext_app_name = APP_INFO["name"]
    shared_dict.app_ext_app_repository_name = get_app_repository(is_air)
    shared_dict.app_ext_version = APP_INFO["shut_down"]["version"]

    return shared_dict


@pytest.fixture(scope="session")
def players(topology_obj):
    return topology_obj.players


@pytest.fixture(scope="session")
def is_simx(platform_params, is_air):
    is_simx_setup = False
    if re.search('simx', platform_params.setup_name):
        is_simx_setup = True
    if is_air:
        is_simx_setup = True
    return is_simx_setup


@pytest.fixture(scope="session")
def is_performance(request):
    is_perf_setup = False
    if re.search('performance', request.config.getoption('--setup_name')):
        is_perf_setup = True
    return is_perf_setup


@pytest.fixture(scope='session')
def is_air(platform_params):
    is_air_setup = False
    if re.search('air', platform_params.setup_name.lower()):
        is_air_setup = True
    return is_air_setup


def cleanup_last_config_in_stack(cleanup_list):
    """
    Execute the last function in the cleanup stack
    :param cleanup_list: list with functions to cleanup
    :return: None
    """
    func, args = cleanup_list.pop()
    func(*args)


@pytest.fixture(scope="session")
def nvos_api_type(request):
    """
    Method for getting nvos_api_type from pytest arguments
    :param request: pytest builtin
    :return: nvos_api_type argument value
    """
    return request.config.getoption('--nvos_api_type')


@pytest.fixture(scope='session')
def engines(topology_obj):
    engines_data = DottedDict()
    for player in topology_obj.players:
        engines_data[player] = topology_obj.players[player]['engine']
    return engines_data


@pytest.fixture(scope='session')
def is_in_mec(engines):
    """Check if DUT is in MEC network (10.220.12.x)"""
    return engines.dut.ip.startswith("10.220.12.")


@pytest.fixture(scope='function', autouse=True)
def test_name(request):
    """
    Method for getting the test name parameter
    :param request: pytest builtin
    :return: the test name, i.e, push_gate
    """
    pytest.test_name = request.node.name
    return pytest.test_name


@pytest.fixture(scope='function', autouse=True)
def disable_loganalyzer(request):
    if request.config.getoption("--disable_loganalyzer", default=False) \
            or "disable_loganalyzer" in request.keywords:
        logging.info("Log analyzer is disabled")
        return True
    return False


@pytest.fixture(scope='function', autouse=True)
def should_skip_bug_handler_action(request, disable_loganalyzer):
    if disable_loganalyzer or request.config.getoption("--skip_bug_handler_action"):
        logger.info('Bug handler WILL skip actions for LA errors')
        return True
    else:
        logger.info('Bug handler will NOT skip actions for LA errors')
        return False


@pytest.fixture(autouse=False)
def setups_list():
    return get_all_setups()


@pytest.fixture()
def show_setup_versions(topology_obj):
    cli_obj: SonicGeneralCliDefault = get_dut_cli_obj_from_topo_obj(topology_obj)

    def _attach_setup_versions():
        cli: SonicGeneralCliDefault = cli_obj
        try:
            with allure.step('get setup versions'):
                outputs = cli.show_setup_versions()
            if outputs:
                with allure.step('attach setup versions outputs to allure report'):
                    allure.attach(outputs, 'setup_versions', allure.attachment_type.TEXT)
        except Exception:
            logger.info('could not get setup versions')

    _attach_setup_versions()
    yield
    _attach_setup_versions()


@pytest.fixture
def register_cleanup(request: pytest.FixtureRequest) -> CleanUpT:
    """
    Fixture for registering cleanup functions
    :param request: pytest builtin
    :return: function for registering cleanup

    Usage:
    ```python
    def my_cleanup():
        print("cleanup")

    def test_my_test():
        # do something
        register_cleanup(my_cleanup)
        # do something
    ```
    for more advanced usage, consider using the `partial` function

    Example:
    ```python
    from functools import partial

    def my_cleanup(engines):
        print("cleanup")

    def test_my_test():
        # do something
        register_cleanup(partial(my_cleanup, engines))
        # do something
    ```
    """
    def _register(fn):
        # Wrap the cleanup function with an allure step
        def wrapped_fn():
            with allure.step("Cleanup stage"):
                fn()

        request.addfinalizer(wrapped_fn)
    return _register


@pytest.fixture
def unregister_cleanup(request: pytest.FixtureRequest) -> CleanUpT:
    """
    Fixture for removing a finalizer from the request
    :param request: pytest builtin
    :return: function for removing a finalizer

    Usage:
    ```python
    def my_cleanup():
        ...

    def test_my_test(register_cleanup, unregister_cleanup):
        # do something
        register_cleanup(my_cleanup)
        # do something
        unregister_cleanup(my_cleanup)
        # do something
    ```
    """
    def _unregister_cleanup(cleanup_func):
        try:
            request.session._setupstate.stack[request.node][0].remove(cleanup_func)
        except ValueError:  # the function pointer is not registered in the stack
            pass
    return _unregister_cleanup


@pytest.fixture(scope='session')
def skynet(request):
    """
    Method for get skynet value from pytest arguments
    :param request: pytest builtin
    :return: True or False, if run is skynet type
    """
    return request.config.getoption('--skynet')


@pytest.fixture(scope='session')
def is_ib_router(request, engines):
    """
    Method for get ib_router value from pytest arguments
    :return: True or False, if run is ib_router type
    """
    return request.config.getoption('--ib_router')
