"""For checking issue state based on supplied issue URL.
"""
import logging
import multiprocessing
import os
import re
import pytest
import glob
import json
import datetime as dt
import yaml
import time
from abc import ABCMeta, abstractmethod
from urllib.parse import urlencode
from perscache import Cache
from devts.infra.tools.redmine.redmine_api import get_issue_list_info
from ngts.helpers.bug_handler.bug_handler_helper import is_current_ver_newer_or_equal_than_fixed_ver
from ngts.helpers.redmine_cache_helper import access_redmine_cache
from ngts.helpers.github_issues_helper import extract_github_issues_info, fetch_github_issues_status

import requests
import six

logger = logging.getLogger(__name__)
cache = Cache()
dir_path = os.path.dirname(os.path.realpath(__file__))
DEFAULT_CONDITIONS_FILE = os.path.join(dir_path, "tests_mark_conditions*.yaml")

logger = logging.getLogger(__name__)


@cache(ttl=dt.timedelta(hours=36))
def get_conditions_list():
    logger.info('Reading conditions data from files')
    conditions_list = list()
    default_conditions_files = glob.glob(DEFAULT_CONDITIONS_FILE)
    conditions_files = [f for f in default_conditions_files if os.path.exists(f)]
    if not conditions_files:
        pytest.fail('There is no conditions files')

    try:
        logger.debug('Trying to load test mark conditions files: {}'.format(conditions_files))
        for conditions_file in conditions_files:
            with open(conditions_file) as f:
                logger.debug('Loaded test mark conditions file: {}'.format(conditions_file))
                conditions = yaml.safe_load(f)
                for key, value in list(conditions.items()):
                    conditions_list.append({key: value})
    except Exception as e:
        logger.error('Failed to load {}, exception: {}'.format(conditions_files, repr(e)), exc_info=True)
        pytest.fail('Loading conditions file "{}" failed. Possibly invalid yaml file.'.format(conditions_files))

    return conditions_list


@cache(ttl=dt.timedelta(hours=36))
def get_conditions_redmine_issues_status():
    logger.info('Reading Redmine Issues Status from API')
    conditions = get_conditions_list()
    ignore_list_string = json.dumps(conditions)
    all_redmine_issues = re.findall(r"https:\/\/redmine\.mellanox\.com\/issues\/(\d+)", ignore_list_string)
    issues_active_status_dict = access_redmine_cache(all_redmine_issues)
    return issues_active_status_dict


@cache(ttl=dt.timedelta(hours=36))
def get_conditions_github_issues_status(proxies_items=()):
    """Resolve every GitHub issue referenced by the conditions files in one bulk query.

    Args:
        proxies_items (tuple): Sorted items of the proxies dict, so that the result can be cached.

    Returns:
        dict: Issue URL to bool, True when the issue is still active. Issues that could
            not be resolved in bulk are absent, and are resolved one at a time instead.
    """
    logger.info('Reading GitHub Issues Status from API')
    conditions = get_conditions_list()
    issue_refs = extract_github_issues_info(json.dumps(conditions))
    return fetch_github_issues_status(issue_refs, proxies=dict(proxies_items))


@cache(ttl=dt.timedelta(hours=36))
def get_conditions_redmine_active_issues_fixed_version():
    logger.info('Reading Redmine Issues information from API')

    issues_active_status_dict = get_conditions_redmine_issues_status()
    active_issue_list = [issue for issue, status in issues_active_status_dict.items() if status]
    issues_info_dict = get_issue_list_info(active_issue_list, limit=len(active_issue_list) + 1)

    active_issues_fixed_version_dict = dict()
    for issue, info in issues_info_dict.items():
        for data in info.get('custom_fields'):
            if data.get("name") == 'Fixed in Version':
                active_issues_fixed_version_dict[issue] = data.get("value", '')
        if issue not in active_issues_fixed_version_dict:
            active_issues_fixed_version_dict[issue] = ''

    return active_issues_fixed_version_dict


class IssueCheckerBase(six.with_metaclass(ABCMeta, object)):
    """Base class for issue checker
    """

    def __init__(self, url):
        self.url = url

    @abstractmethod
    def is_active(self):
        """
        Check if the issue is still active
        """
        return True


class RedmineIssueChecker(IssueCheckerBase):
    """
    Redmine issue state checker
    """
    NAME = 'Redmine'

    def __init__(self, url):
        super(RedmineIssueChecker, self).__init__(url)

    def is_active(self):
        """Check if the issue is still active.
        If unable to get issue state, always consider it as active.
        Returns:
            bool: False if the issue is closed else True.
        """
        redmine_issues_status = get_conditions_redmine_issues_status()
        issue_id = self.url.split('/issues/')[1]
        is_issue_active = redmine_issues_status[str(issue_id)]
        return is_issue_active


def check_if_current_ver_include_bug_fix(issue_url, basic_facts):
    start_time = time.perf_counter()
    current_ver_include_bug_fix = False
    issue_id = int(re.match(r"https:\/\/redmine\.mellanox\.com\/issues\/(\d+)", issue_url)[1])

    active_issues_fixed_version_dict = get_conditions_redmine_active_issues_fixed_version()
    fixed_in_version = active_issues_fixed_version_dict.get(issue_id, '')
    fixed_in_version = '' if fixed_in_version is None else fixed_in_version
    logger.info(f"Fixed ver is:{fixed_in_version}")

    branch = basic_facts.get('branch', 'none')
    cur_version = basic_facts.get('build_version', '')
    if f'{branch}.' in fixed_in_version:
        if is_current_ver_newer_or_equal_than_fixed_ver(branch, cur_version, fixed_in_version):
            logger.info(f"The current image:{cur_version} has included the bug fix {issue_url}."
                        f" Bug fix start form :{fixed_in_version}")
            current_ver_include_bug_fix = True
    end_time = time.perf_counter()
    logger.info(f"Execution Time: {end_time - start_time} seconds")
    return current_ver_include_bug_fix


class GitHubIssueChecker(IssueCheckerBase):
    """GitHub issue state checker
    """

    NAME = 'GitHub'

    def __init__(self, url, proxies):
        super(GitHubIssueChecker, self).__init__(url)
        self.api_url = url.replace('github.com', 'api.github.com/repos')
        self.proxies = proxies

    def is_active(self):
        """Check if the GitHub issue is still active.

        Prefer the state from the once per session bulk query, falling back to a single
        request for URLs it did not resolve.

        Returns:
            bool: False if the issue is closed else True.
        """
        github_issues_status = get_conditions_github_issues_status(tuple(sorted((self.proxies or {}).items())))
        if self.url in github_issues_status:
            return github_issues_status[self.url]

        return self._is_active_via_rest()

    def _is_active_via_rest(self):
        """Check if the GitHub issue is still active using one REST request.

        Attempt to fetch issue details via proxy if configured. If proxy fails, retry with direct GitHub API URL.
        If unable to retrieve issue state, assume the issue is active (safe default).

        Returns:
            bool: False if the issue is closed else True.
        """

        def fetch_issue(url):
            response = requests.get(url, proxies=self.proxies, timeout=10)
            response.raise_for_status()
            return response.json()

        direct_url = self.api_url
        proxy_url = os.getenv("SONIC_AUTOMATION_PROXY_GITHUB_ISSUES_URL")

        issue_data = None

        # Attempt to access via proxy first (if configured)
        # The proxy is used to work around GitHub's unauthenticated rate limit (60 requests/hour per IP).
        # For details, refer to GitHub API rate limits documentation:
        # https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2022-11-28#primary-rate-limit-for-unauthenticated-users
        if proxy_url:
            try:
                proxy_endpoint = f"{proxy_url.rstrip('/')}/?{urlencode({'github_issue_url': direct_url})}"
                logger.info("Attempting to access GitHub API via proxy.")
                issue_data = fetch_issue(proxy_endpoint)
            except Exception as proxy_err:
                logger.warning(f"Proxy access failed: {proxy_err}. Falling back to direct API.")

        # Fallback to direct URL if proxy is not set or fails
        if issue_data is None:
            try:
                logger.info(f"Accessing GitHub API directly: {direct_url}")
                issue_data = fetch_issue(direct_url)
            except Exception as direct_err:
                logger.error(f"Access GitHub API directly failed for {direct_url}: {direct_err}")
                logger.debug(f"Issue {direct_url} is considered active due to API access failure.")
                return True

        # Check issue state
        if issue_data.get('state') == 'closed':
            logger.debug(f"Issue {direct_url} is closed.")
            labels = issue_data.get('labels', [])
            if any('name' in label and 'duplicate' in label['name'].lower() for label in labels):
                logger.warning(
                    f"GitHub issue {direct_url} appears to be a duplicate and was closed. "
                    f"Consider ignoring related test failures.")
            return False

        logger.debug(f"Issue {direct_url} is active.")
        return True


def issue_checker_factory(url, proxies):
    """Factory function for creating issue checker object based on the domain name in the issue URL.

    Args:
        url (str): Issue URL.

    Returns:
        obj: An instance of issue checker.
    """
    m = re.match('https?://([^/]+)', url)
    if m and len(m.groups()) > 0:
        domain_name = m.groups()[0].lower()
        if 'github' in domain_name:
            return GitHubIssueChecker(url, proxies)
        elif 'redmine' in domain_name:
            return RedmineIssueChecker(url)
        else:
            logger.error('Unknown issue website: {}'.format(domain_name))
    logger.error('Creating issue checker failed. Bad issue url {}'.format(url))
    return None


def check_issues(issues, proxies=None):
    """Check state of the specified issues.

    Because issue state checking may involve sending HTTP request. This function uses parallel run to speed up
    issue status checking.

    The GitHub status cache is filled in this process first, so each forked checker
    reads the same perscache entry instead of sending its own bulk query.

    Args:
        issues (list of str): List of issue URLs.

    Returns:
        dict: Issue state check result. Key is issue URL, value is either True or False based on issue state.
    """
    checkers = [c for c in [issue_checker_factory(issue, proxies) for issue in issues] if c is not None]
    if not checkers:
        logger.error('No checker created for issues: {}'.format(issues))
        return {}

    get_conditions_github_issues_status(tuple(sorted((proxies or {}).items())))

    check_results = multiprocessing.Manager().dict()
    check_procs = []

    def _check_issue(checker, results):
        results[checker.url] = checker.is_active()

    for checker in checkers:
        check_procs.append(multiprocessing.Process(target=_check_issue, args=(checker, check_results,)))

    for proc in check_procs:
        proc.start()
    for proc in check_procs:
        proc.join(timeout=120)

    return dict(check_results)
