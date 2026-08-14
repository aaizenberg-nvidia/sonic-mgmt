import logging
import glob
import pytest
import yaml
import os
import re
import requests
import subprocess
import json
import datetime as dt
from abc import ABCMeta, abstractmethod
from perscache import Cache
from ngts.helpers.redmine_cache_helper import access_redmine_cache
from ngts.constants.constants import CliType, CLI_TYPE_CACHE_PATH

cache = Cache()

logger = logging.getLogger()


class DynamicLaConsts:
    CUSTOM_TEST_SKIP_PLATFORM_TYPE = 'dynamic_tests_skip_platform_type'
    CUSTOM_TEST_SKIP_BRANCH_NAME = 'dynamic_tests_skip_branch_name'
    CUSTOM_TEST_SKIP_IMAGE_TYPE = "dynamic_tests_skip_image_type"
    LA_DYNAMIC_IGNORES_LIST = 'LA_DYNAMIC_IGNORES_LIST'
    ERRORS_LIST = 'Errors_list'
    REDMINE = 'Redmine'
    PLATFORM = 'Platform'
    AFFECTED_TEST_CASES = 'Affected_test_cases'
    CONDITIONS = 'Conditions'
    BRANCH = 'Branch'
    GITHUB = 'GitHub'
    IMAGE_TYPE = 'Image_type'


def pytest_collection(session):
    initialize_cached_variables(session)


def initialize_cached_variables(session):
    session.config.cache.set(DynamicLaConsts.LA_DYNAMIC_IGNORES_LIST, None)


def pytest_runtest_setup(item):
    if (item.config.getoption('disable_loganalyzer') or "disable_loganalyzer" in item.keywords) \
            and not item.config.getoption('force_load_err_list'):
        return
    extended_ignore_list = get_extended_ignore_list(item)
    item.session.config.cache.set("extended_ignore_list", extended_ignore_list)


def pytest_runtest_teardown(item):
    item.session.config.cache.set("extended_ignore_list", [])


def get_cli_type():
    try:
        cli_type = os.environ.get("CLI_TYPE")
        if not cli_type:
            logger.info("CLI_TYPE not found in environment variables, getting from cache")
            with open(CLI_TYPE_CACHE_PATH, 'r') as f:
                cli_type = json.load(f)
        return cli_type
    except Exception as e:
        logger.info(f"An error occurred while getting CLI type: {e}")
        return ''


def get_extended_ignore_list(item):
    extended_ignore_list = []

    cli_type = get_cli_type()
    dynamic_la_ignore_list = get_ignore_list(cli_type)
    for ignore_block in dynamic_la_ignore_list:

        errors_regexp_to_be_ignored_list = ignore_block.get(DynamicLaConsts.ERRORS_LIST)
        if not errors_regexp_to_be_ignored_list:
            raise Exception('Errors list not provided for dynamic LA errors ignore. Check YAML file.')
        conditions_dict = ignore_block.get(DynamicLaConsts.CONDITIONS)
        if not conditions_dict:
            raise Exception('LA dynamic errors ignore condition not provided in ignore block: {}.'
                            'Check YAML file and fix it'.format(ignore_block))

        for condition_dict_entry in conditions_dict:
            operand = 'and' if is_nested_dict(condition_dict_entry) else 'or'
            is_error_ignore_required = get_checkers_result(condition_dict_entry, item, operand=operand)
            if is_error_ignore_required:
                extended_ignore_list.extend(errors_regexp_to_be_ignored_list)
                # Found match on the first condition block, no need to check others
                break
    return extended_ignore_list


@cache(ttl=dt.timedelta(hours=36))
def get_ignore_list(cli_type):
    logger.info('Reading dynamic errors ignore data from file')
    ignore_list = list()
    la_dynamic_ignore_folder_path = os.environ.get("DYNAMIC_INGNORE_PATH")
    if not la_dynamic_ignore_folder_path:
        la_dynamic_ignore_folder_path = os.path.dirname(__file__)
    logger.info(f'CLI type: {cli_type}')
    if cli_type == CliType.SONIC:
        la_ignore_file = "dynamic_loganalyzer_ignores.yaml"  # SpellCheck-ignoreLine
    else:
        la_ignore_file = "dynamic_loganalyzer_ignores*.yaml"  # SpellCheck-ignoreLine
    path_to_dynamic_la_ignore_file = os.path.join(la_dynamic_ignore_folder_path, la_ignore_file)
    ignore_files_list = glob.glob(path_to_dynamic_la_ignore_file)
    ignore_files = [f for f in ignore_files_list if os.path.exists(f)]
    if not ignore_files:
        pytest.fail('There is no ignore file')

    try:
        logger.info('Trying to load loganalyzer ignore files: {}'.format(ignore_files))
        for ignore_file in ignore_files:
            with open(ignore_file) as dynamic_la_ignore_obj:
                ignore_data = yaml.load(dynamic_la_ignore_obj, Loader=yaml.FullLoader)
                ignore_list.extend(ignore_data)
    except Exception as e:
        logger.error('Failed to load {}, exception: {}'.format(ignore_files, repr(e)), exc_info=True)
        pytest.fail('Loading ignore file "{}" failed. Possibly invalid yaml file.'.format(ignore_files))

    return ignore_list


@cache(ttl=dt.timedelta(hours=36))
def get_redmine_issues_status(cli_type):
    logger.info('Reading Redmine Issues Status from API')
    ignore_list = get_ignore_list(cli_type)
    ignore_list_string = json.dumps(ignore_list)
    all_redmine_issues = re.findall(r"\"Redmine\":\s*\[(\d+)\]", ignore_list_string)
    issues_active_status_dict = access_redmine_cache(all_redmine_issues, use_active_status=True)
    return issues_active_status_dict


def is_nested_dict(dict_obj):
    nested_dict_min_len = 2
    return len(dict_obj) >= nested_dict_min_len


def get_checkers_result(condition_dict_entry, item, operand='or'):
    """
    Check if errors should be added to ignored errors. Check conditions based operand.
    If operand "or" - check if one condition matched - then return True
    If operand "and" - check if all conditions matched - then return True, if some condition not matched and operand
    "and" - break(do not run all other checkers for save time) and return False
    :param condition_dict_entry: dictionary with conditions which should be checked - dynamic_loganalyzer_ignores.yaml
    :param item: pytest build-in
    :param operand: operand - "or" or "and"
    :return:
    """
    available_checkers = {DynamicLaConsts.AFFECTED_TEST_CASES: AffectedTestCaseDynamicErrorsIgnore,
                          DynamicLaConsts.PLATFORM: PlatformDynamicErrorsIgnore,
                          DynamicLaConsts.BRANCH: BranchDynamicErrorsIgnore,
                          DynamicLaConsts.IMAGE_TYPE: ImageTypeDynamicErrorsIgnore,
                          DynamicLaConsts.REDMINE: RedmineDynamicErrorsIgnore,
                          DynamicLaConsts.GITHUB: GitHubDynamicErrorsIgnore}
    if not item:
        available_checkers = {DynamicLaConsts.REDMINE: RedmineDynamicErrorsIgnore, 
                              DynamicLaConsts.GITHUB: GitHubDynamicErrorsIgnore}
    checkers_result = []

    # Run the less time-consuming checkers first
    checkers_ordered_by_prio_list = [DynamicLaConsts.AFFECTED_TEST_CASES, DynamicLaConsts.PLATFORM,
                                     DynamicLaConsts.IMAGE_TYPE, DynamicLaConsts.BRANCH, DynamicLaConsts.REDMINE,
                                     DynamicLaConsts.GITHUB]

    for checker in checkers_ordered_by_prio_list:
        if checker in condition_dict_entry:
            if checker in available_checkers:
                checker_obj = available_checkers[checker](condition_dict_entry, item)
                is_checker_matched = checker_obj.is_checker_match()
            else:
                is_checker_matched = True
            checkers_result.append(is_checker_matched)
            # Do not continue if operand "and" and we already have failed checker
            if not is_checker_matched and operand == 'and':
                break

    if operand == 'or':
        ignore_error_required = any(checkers_result)
    else:
        ignore_error_required = all(checkers_result)

    return ignore_error_required


def run_cmd_on_dut(pytest_item_obj, cmd):
    """
    Run command on DUT using ansible and return output
    """
    host = pytest_item_obj.session.config.option.ansible_host_pattern
    inventory = pytest_item_obj.session.config.option.ansible_inventory
    inv = get_inventory_argument(inventory)
    output = subprocess.check_output('ansible {} {} -a "{}"'.format(host, inv, cmd), shell=True)
    return output


def get_inventory_argument(inventory):
    """
    Get Ansible inventory arguments
    """
    inv = ''

    if isinstance(inventory, list):
        for inv_item in inventory:
            inv += ' -i {}'.format(inv_item)
    else:
        for inv_item in inventory.split(','):
            inv += ' -i {}'.format(inv_item)

    return inv


class LaDynamicErrorsIgnore:
    __metaclass__ = ABCMeta

    def __init__(self, conditions_dict, pytest_item_obj):
        # self.name = 'CustomSkipIf'  # Example: Platform, Jira, Redmine - should be defined in each child class
        self.conditions_dict = conditions_dict
        self.pytest_item_obj = pytest_item_obj

    @abstractmethod
    def is_checker_match(self):
        """
        Decide whether or not to add ignore for errors
        :return: True/False
        """
        pass


class AffectedTestCaseDynamicErrorsIgnore(LaDynamicErrorsIgnore):
    def __init__(self, conditions_dict, pytest_item_obj):
        super(AffectedTestCaseDynamicErrorsIgnore, self).__init__(conditions_dict, pytest_item_obj)
        self.validation_name = DynamicLaConsts.AFFECTED_TEST_CASES

    def is_checker_match(self):
        is_errors_ignore_required = True

        if self.conditions_dict.get(self.validation_name):
            is_errors_ignore_required = False
            for test_prefix in self.conditions_dict[self.validation_name]:
                if str(self.pytest_item_obj.nodeid).startswith(test_prefix):
                    is_errors_ignore_required = True
                    break

        return is_errors_ignore_required


class BranchDynamicErrorsIgnore(LaDynamicErrorsIgnore):
    def __init__(self, conditions_dict, pytest_item_obj):
        super(BranchDynamicErrorsIgnore, self).__init__(conditions_dict, pytest_item_obj)
        self.current_branch = self.get_branch_name()
        self.validation_name = DynamicLaConsts.BRANCH

    def get_branch_name(self):
        """
        Get current branch name using ansible and store it in pytest.session.config.cache
        :return: platform_type - string with current branch name
        """
        branch_name = self.pytest_item_obj.session.config.cache.get(DynamicLaConsts.CUSTOM_TEST_SKIP_BRANCH_NAME, None)
        if not branch_name:
            logger.debug('Getting branch name from DUT')
            try:
                release_output = run_cmd_on_dut(self.pytest_item_obj,
                                                "sonic-cfggen -y /etc/sonic/sonic_version.yml -v release").strip()
                if isinstance(release_output, bytes):
                    release_output = release_output.decode("utf-8")
                branch_name = self.get_branch_from_release_output(release_output)
                self.pytest_item_obj.session.config.cache.set(DynamicLaConsts.CUSTOM_TEST_SKIP_BRANCH_NAME, branch_name)
            except Exception as err:
                logger.error('Unable to get branch name. Custom skip by branch impossible. Error: {}'.format(err))
        else:
            logger.debug('Getting branch from pytest cache')

        logger.debug('Current branch is: {}'.format(branch_name))
        return branch_name

    @staticmethod
    def get_branch_from_release_output(release_output):
        """
        Get branch name from "sonic-cfggen -y /etc/sonic/sonic_version.yml -v release" output
        :param release_output: output of ansible command "sonic-cfggen -y /etc/sonic/sonic_version.yml -v release"
        :return: string with branch name, example: '202012'
        example of release_output:
            'r-lionfish-13 | CHANGED | rc=0 >>\nnone'
            'r-lionfish-13 | CHANGED | rc=0 >>\n202012'
        """
        branch_name = release_output.splitlines()[1]
        # master branch always has release "none"
        if branch_name == "none":
            branch_name = "master"
        return str(branch_name)

    def is_checker_match(self):
        is_errors_ignore_required = True

        if self.conditions_dict.get(self.validation_name):
            is_errors_ignore_required = False
            for branch in self.conditions_dict[self.validation_name]:
                if str(branch) == self.current_branch:
                    is_errors_ignore_required = True
                    break

        return is_errors_ignore_required


class PlatformDynamicErrorsIgnore(LaDynamicErrorsIgnore):
    def __init__(self, conditions_dict, pytest_item_obj):
        super(PlatformDynamicErrorsIgnore, self).__init__(conditions_dict, pytest_item_obj)
        self.current_platform = self.get_platform_type()
        self.validation_name = DynamicLaConsts.PLATFORM

    def get_platform_type(self):
        """
        Get current platform type using ansible and store it in pytest.session.config.cache
        :return: platform_type - string with current platform type
        """
        platform_type = self.pytest_item_obj.session.config.cache.get(DynamicLaConsts.CUSTOM_TEST_SKIP_PLATFORM_TYPE,
                                                                      None)
        if not platform_type:
            logger.debug('Getting platform from DUT')
            try:
                show_platform_summary_raw_output = run_cmd_on_dut(self.pytest_item_obj,
                                                                  'show platform summary').decode()
                platform_type = self.get_platform_from_platform_summary(show_platform_summary_raw_output)
                self.pytest_item_obj.session.config.cache.set(DynamicLaConsts.CUSTOM_TEST_SKIP_PLATFORM_TYPE,
                                                              platform_type)
            except Exception as err:
                logger.error('Unable to get platform type. Custom skip by platform impossible. Error: {}'.format(err))
        else:
            logger.debug('Getting platform from pytest cache')

        logger.debug('Current platform type is: {}'.format(platform_type))
        return platform_type

    @staticmethod
    def get_platform_from_platform_summary(platform_output):
        """
        Get platform from 'show platform summary' output
        :param platform_output: 'show platform summary' command output
        :return: string with platform name, example: 'x86_64-mlnx_msn3420-r0'
        """
        platform = re.search(r'Platform:\s(.*)', platform_output, re.IGNORECASE).group(1)
        return platform

    def is_checker_match(self):
        is_errors_ignore_required = True

        if self.conditions_dict.get(self.validation_name):
            is_errors_ignore_required = False
            for platform in self.conditions_dict[self.validation_name]:
                if str(platform) in self.current_platform:
                    is_errors_ignore_required = True
                    break

        return is_errors_ignore_required


class RedmineDynamicErrorsIgnore(LaDynamicErrorsIgnore):
    def __init__(self, conditions_dict, pytest_item_obj):
        super(RedmineDynamicErrorsIgnore, self).__init__(conditions_dict, pytest_item_obj)
        self.validation_name = DynamicLaConsts.REDMINE

    def is_checker_match(self):
        is_errors_ignore_required = True
        if self.conditions_dict.get(self.validation_name):
            is_errors_ignore_required = False
            rm_issues_list = self.conditions_dict[self.validation_name]
            cli_type = get_cli_type()
            redmine_issues_status = get_redmine_issues_status(cli_type)
            is_issue_active = any([redmine_issues_status[str(issue)] for issue in rm_issues_list])
            if is_issue_active:
                is_errors_ignore_required = True

        return is_errors_ignore_required


class GitHubDynamicErrorsIgnore(LaDynamicErrorsIgnore):
    def __init__(self, conditions_dict, pytest_item_obj):
        super(GitHubDynamicErrorsIgnore, self).__init__(conditions_dict, pytest_item_obj)
        self.validation_name = DynamicLaConsts.GITHUB
        self.name = 'GitHub'
        self.github_username = os.getenv("GITHUB_USER")
        self.api_token = os.getenv("GITHUB_API_TOKEN")
        self.auth = (self.github_username, self.api_token)

    @staticmethod
    def get_github_issue_api_url(issue_url):
        """
        Get correct github api URL based on browser URL from user
        :param issue_url: github issue url
        :return: github issue api url
        """
        return issue_url.replace('github.com', 'api.github.com/repos')

    def make_github_request(self, url):
        """
        Send API request to github
        :param url: github api url
        :return: dictionary with data
        """
        response = requests.get(url, auth=self.auth, timeout=30)
        response.raise_for_status()
        return response.json()

    def is_github_issue_active(self, issue_url):
        """
        Check that issue active or not
        :param issue_url:  github issue URL
        :return: True/False
        """
        try:
            issue_url = self.get_github_issue_api_url(issue_url)
            response = self.make_github_request(issue_url)

            if response.get('state') == 'closed':
                if self.is_duplicate(response):
                    logger.warning('GitHub issue: {} looks like a duplicate and was closed. '
                                   'Please re-check and ignore the test on the parent issue.'.format(issue_url))
                return False

            return True

        except Exception as e:
            logger.error(f"An error occurred while checking GitHub issue: {e}")
            return False

    @staticmethod
    def is_duplicate(issue_data):
        """
        Check if issue duplicate or note
        :param issue_data: github response dict
        :return: True/False
        """
        for label in issue_data['labels']:
            if 'duplicate' in label['name'].lower():
                return True
        return False

    def is_checker_match(self):
        is_errors_ignore_required = True

        if self.conditions_dict.get(self.validation_name):
            is_errors_ignore_required = False

            for github_issue in self.conditions_dict[self.validation_name]:
                if self.is_github_issue_active(github_issue):
                    is_errors_ignore_required = True
                    break

        return is_errors_ignore_required


class ImageTypeDynamicErrorsIgnore(LaDynamicErrorsIgnore):
    def __init__(self, conditions_dict, pytest_item_obj):
        super(ImageTypeDynamicErrorsIgnore, self).__init__(conditions_dict, pytest_item_obj)
        self.validation_name = DynamicLaConsts.IMAGE_TYPE
        self.current_image = self.get_image()

    def is_checker_match(self):
        is_errors_ignore_required = True

        if self.conditions_dict.get(self.validation_name):
            is_errors_ignore_required = False
            image_type_list = self.conditions_dict[self.validation_name]
            for image_type in image_type_list:
                if str(image_type).lower() in self.current_image.lower():  # .lower to make it case-insensitive
                    is_errors_ignore_required = True
                    break
        return is_errors_ignore_required

    def get_image(self):
        """
        Get current image using ansible and store it in pytest.session.config.cache
        :return: image - string with current image type
        """
        image = self.pytest_item_obj.session.config.cache.get(DynamicLaConsts.CUSTOM_TEST_SKIP_IMAGE_TYPE, None)
        if not image:
            logger.debug('Getting image from DUT')
            try:
                sonic_installer_list_raw_output = run_cmd_on_dut(self.pytest_item_obj,
                                                                 'sudo sonic-installer list').decode()
                image = self.get_image_from_sonic_installer_list(sonic_installer_list_raw_output)
                self.pytest_item_obj.session.config.cache.set(DynamicLaConsts.CUSTOM_TEST_SKIP_IMAGE_TYPE,
                                                              image)
            except Exception as err:
                logger.error('Unable to get image type. Custom skip by image impossible. Error: {}'.format(err))
        else:
            logger.debug('Getting image from pytest cache')

        logger.debug('Current image type is: {}'.format(image))
        return image

    @staticmethod
    def get_image_from_sonic_installer_list(sonic_installer_output):
        """
        Get image from 'sudo sonic-installer list' output
        :param sonic_installer_output: 'sudo sonic-installer list' command output
        :return: string with image, example: 'SONiC-OS-202311_RC.15-271579723_Internal'
        """
        image = re.search(r'Current:\s(.*)', sonic_installer_output, re.IGNORECASE).group(1)
        return image
