import json
import logging

from ngts.constants.constants import Sonic_Cache
from devts.infra.tools.redmine.redmine_api import get_issues_active_status

logger = logging.getLogger(__name__)

UNKNOWN_ISSUE_ACTIVE_STATUS = True


def access_redmine_cache(redmine_issues_list):
    """
    Try to get issues status from cache first, if not available or missing issues, get from API.

    Args:
        redmine_issues_list (list): List of issue IDs to check
    returns:
        dict: Dictionary of issue ID as a string -> is issue active
    """
    try:
        logger.info(f"Reading from cache: {Sonic_Cache.REDMINE_ISSUES_STATUS_CACHE}")
        with open(Sonic_Cache.REDMINE_ISSUES_STATUS_CACHE, "r") as file:
            redmine_cache_issues = json.load(file)
    except Exception as err:
        logger.warning(f"Access to cache {Sonic_Cache.REDMINE_ISSUES_STATUS_CACHE} unsuccessful: {err}")
        redmine_cache_issues = {}

    issues_status_dict = {}
    missing_issues = []
    for issue in redmine_issues_list:
        if str(issue) in redmine_cache_issues:
            issues_status_dict[str(issue)] = redmine_cache_issues[str(issue)]
        else:
            missing_issues.append(issue)

    if missing_issues:
        logger.info(f"Getting status from Redmine API for the issues which are not in cache: {missing_issues}")
        try:
            issues_status_dict.update({str(issue): status for issue, status
                                       in get_issues_active_status(missing_issues).items()})
        except Exception as err:
            logger.error(f"Failed to get status from Redmine API for issues {missing_issues}: {err}")
        # If the Redmine API returns an error, the issues_status_dict may be empty or partially filled
        for issue in missing_issues:
            if str(issue) not in issues_status_dict:
                logger.warning(f"Status of issue {issue} is unknown, assuming {UNKNOWN_ISSUE_ACTIVE_STATUS}")
                issues_status_dict[str(issue)] = UNKNOWN_ISSUE_ACTIVE_STATUS

    return issues_status_dict
