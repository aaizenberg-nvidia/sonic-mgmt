import os
import sys
import allure
import re
import logging

from ngts.constants.constants import PytestConst
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
logger = logging.getLogger()


class SupportedRebootReloadTypes:
    def __init__(self, platform=None):
        self.reboot = 'reboot'
        self.fast_reboot = 'fast-reboot'
        self.warm_reboot = 'warm-reboot'
        self.config_reload = 'config reload -y'
        if platform and '4280' in platform:
            del self.fast_reboot
            del self.warm_reboot
        if platform and 'sn6600_ld' in platform:
            if is_redmine_issue_active([5042499])[0]:
                del self.fast_reboot
                logger.info('Redmine #5042499 is active, removing fast-reboot from the list of supported reboot types')
            if is_redmine_issue_active([5154543])[0]:
                del self.warm_reboot
                logger.info('Redmine #5154543 is active, removing warm reboot from the list of supported reboot types')
        if platform and 'sn6810_ld' in platform:
            if is_redmine_issue_active([4799042])[0]:
                del self.fast_reboot
                logger.info('Redmine #4799042 is active, removing fast-reboot from the list of supported reboot types')
            if is_redmine_issue_active([5343702])[0]:
                del self.warm_reboot
                logger.info('Redmine #5343702 is active, removing warm reboot from the list of supported reboot types')


def get_supported_reboot_reload_types_list(platform=None):
    """
    Get list of supported reboot/reload types
    :param platform: platform, example: x86_64-mlnx_msn2010-r0
    :return: list with supported reboot types, example: ['reboot', 'warm-reboot', 'config reload -y']
    """
    supported_reboot_reload_types_list = list(SupportedRebootReloadTypes(platform).__dict__.values())
    return supported_reboot_reload_types_list


def add_to_pytest_args_skip_tests(pytest_args_list, skip_test_case_list):
    """
    This method adds ignore parameter for the test_push_gate_reboot_policer and other tests.
    We need to ignore test_push_gate_reboot_policer - otherwise the Reload tests will be called in an endless loop
    :param pytest_args_list: list with pytest arguments
    :param skip_test_case_list: test case name list which will be skipped
    :return: modified list with pytest arguments
    """
    keyword_expression_arg = '-k'
    skip_arg_list = [f'not {skip_test_case}' for skip_test_case in skip_test_case_list]
    skip_arg = " and ".join(skip_arg_list)
    if keyword_expression_arg not in pytest_args_list:
        pytest_args_list.insert(-1, keyword_expression_arg)
        pytest_args_list.insert(-1, '"{}"'.format(skip_arg))
    else:
        index_k = pytest_args_list.index(keyword_expression_arg)
        available_data = pytest_args_list[index_k + 1]
        pytest_args_list.remove(available_data)
        available_data += ' and {}'.format(skip_arg)
        pytest_args_list.insert(index_k + 1, '"{}"'.format(available_data))

    return pytest_args_list


def add_to_pytest_args_disable_loganalyzer(pytest_args_list):
    """
    This method adds disable_loganalyzer parameter for pytest run args
    :param pytest_args_list: list with pytest arguments
    :return: modified list with pytest arguments
    """
    if PytestConst.disable_loganalyzer not in pytest_args_list:
        pytest_args_list.insert(-1, PytestConst.disable_loganalyzer)

    return pytest_args_list


def add_to_pytest_args_disable_exporting_test_results_to_mars_db(pytest_args_list):
    """
    This method adds disable_export_mars_cases_result parameter for pytest run args
    :param pytest_args_list: list with pytest arguments
    :return: modified list with pytest arguments
    """
    if PytestConst.disable_export_mars_cases_result not in pytest_args_list:
        pytest_args_list.insert(-1, PytestConst.disable_export_mars_cases_result)

    return pytest_args_list


def remove_allure_server_project_id_arg(pytest_args_list):
    """
    This method removes the allure project id argument, since there is no need to maintain history for reboot validations
    :param pytest_args_list: list with pytest arguments
    :return: modified list with pytest arguments
    """
    pytest_args_list = remove_allure_server_project_id_arg_space_separator(pytest_args_list)
    pytest_args_list = remove_allure_server_project_id_arg_eq_separator(pytest_args_list)
    return pytest_args_list


def remove_allure_server_project_id_arg_eq_separator(pytest_args_list):
    """
    This method removes the allure project id arg, if given with an eq separator, for example:
    --allure_server_project_id=abcd
    :param pytest_args_list: list with pytest arguments
    :return: modified list with pytest arguments
    """
    allure_server_project_arg = '--allure_server_project_id='
    return [arg for arg in pytest_args_list if not arg.startswith(allure_server_project_arg)]


def remove_allure_server_project_id_arg_space_separator(pytest_args_list):
    """
    This method removes the allure project id arg, if given with an eq separator, for example:
    --allure_server_project_id abcd
    :param pytest_args_list: list with pytest arguments
    :return: modified list with pytest arguments
    """
    allure_server_project_arg = '--allure_server_project_id'
    if allure_server_project_arg in pytest_args_list:
        allure_server_project_arg_index = pytest_args_list.index(allure_server_project_arg)
        # Delete the allure project dir arg and value, a random allure project will be generated by the plugin
        del pytest_args_list[allure_server_project_arg_index]
        del pytest_args_list[allure_server_project_arg_index]
    return pytest_args_list


def prepare_pytest_cmd_with_custom_allure_dir(pytest_args_list, allure_dir):
    """
    This method appends the custom alluredir folder argument to the pytest cmd
    :param pytest_args_list: list with pytest arguments
    :param allure_dir: allure dir
    :return: pytest run cmd(example: 'pytest --run_test_only --setup_name=sonic_spider_r-spider-05
    --rootdir=/local/repos/sonic-mgmt/ngts -c /local/repos/sonic-mgmt/ngts/pytest.ini --log-level=INFO
    --clean-alluredir --alluredir=/tmp/allure_reboot_reload -k "not test_push_gate_reboot_policer"
    /local/repos/sonic-mgmt/ngts/tests/push_build_tests')
    """
    python_bin_folder = os.path.dirname(sys.executable)
    pytest_path = os.path.join(python_bin_folder, 'pytest')
    cmd = '{} {} '.format(pytest_path, PytestConst.run_test_only_arg)
    for arg in pytest_args_list:
        if '{}='.format(PytestConst.alluredir_arg) in arg:
            arg = '{}={}'.format(PytestConst.alluredir_arg, allure_dir)
        cmd += '{} '.format(arg)

    return cmd


def generate_report(out, err):
    """
    This method generates report for functional validation step, it will attach allure url and logs to allure report
    :param out: pytest stdout
    :param err: std error
    """
    allure.attach(out, 'stdout', allure.attachment_type.TEXT)
    allure.attach(err, 'stderr', allure.attachment_type.TEXT)
    try:
        allure_report_url = re.search(r'Allure\sreport\sURL\:\s(http://.*)', out.decode('utf-8')).group(1)
        logger.info('Found functional validation Allure report URL: {}'.format(allure_report_url))
        allure.attach(bytes(allure_report_url, 'utf-8'), 'Allure report URL', allure.attachment_type.URI_LIST)
    except Exception as err:
        logger.error('Can not find and attach allure URL to allure report. Error: {}'.format(err))


def add_to_pytest_args_custom_cache_dir(pytest_args_list):
    """
    This method adds custom pytest cache folder argument to pytest arguments list
    It's required to prevent case when we rewrite some values in parent pytest session cache
    :param pytest_args_list: list with pytest arguments
    :return: modified list with pytest arguments
    """
    pytest_args_list.insert(0, '-o cache_dir=/tmp/.pytest_cache')
    return pytest_args_list
