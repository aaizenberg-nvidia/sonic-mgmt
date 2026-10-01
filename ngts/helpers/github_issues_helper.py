"""Bulk resolution of GitHub issue state for the conditional_mark plugin.

The conditional mark files reference a few hundred unique issues, too many for
the REST rate limit, so they are resolved with a single GraphQL query that
aliases every issue. Issues left out of the returned dict were not resolved and
fall back to the caller's per-issue REST query.
"""
import logging
import os
import time
from collections import OrderedDict
from typing import NamedTuple
import re

import requests

logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

GITHUB_ISSUE_URL_RE = re.compile(
    r"https://github\.com/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"issues/(?P<number>\d+)"
)

GITHUB_TIMEOUT_STATUS_CODES = (502, 504)
GITHUB_RESOURCE_LIMIT_ERROR_TYPE = "RESOURCE_LIMITS_EXCEEDED"
GITHUB_RESOURCE_LIMIT_MESSAGE = "resource limits for this query exceeded"
SECONDARY_RATE_LIMIT_RE = re.compile(r"\b" r"secondary rate" r"\b", re.IGNORECASE)
RATE_LIMIT_FIELD = "  rateLimit { cost limit remaining used nodeCount }"

MAX_RETRIES = 3
BACKOFF_FACTOR = 1.0
MAX_BACKOFF_SECONDS = 60.0
REQUEST_TIMEOUT = 60


class GitHubResourceLimitError(Exception):
    """Raised when GitHub rejects a GraphQL query for exceeding resource limits."""


class GitHubIssueInfo(NamedTuple):
    """A GitHub issue reference, keeping the URL verbatim as callers key on it."""

    owner: str
    repo: str
    number: int
    url: str

    @property
    def repo_key(self):
        """Case insensitive repository identity, as GitHub names are case insensitive."""
        return self.owner.lower(), self.repo.lower()


def extract_github_issues_info(text):
    """Find every GitHub issue reference in a blob of text.

    Args:
        text (str): Arbitrary text, for example the JSON dump of the conditions.

    Returns:
        list: Sorted unique GitHubIssueInfo, one per distinct URL.
    """
    refs = {}
    for match in GITHUB_ISSUE_URL_RE.finditer(text):
        url = match.group(0)
        if url in refs:
            continue
        refs[url] = GitHubIssueInfo(owner=match.group("owner"), repo=match.group("repo"),
                                    number=int(match.group("number")), url=url)
    return sorted(refs.values())


def fetch_github_issues_status(issues, proxies=None):
    """Resolve the state of many GitHub issues with as few requests as possible.

    Too-expensive batches are retried in smaller parts; leftovers fall back to REST.

    Args:
        issues (list): List of GitHubIssueInfo to resolve.
        proxies (dict): Proxy mapping for requests, as used by the REST path.

    Returns:
        dict: Issue URL to bool, True when the issue is still active. Unresolved
            issues are absent from the dict.
    """
    sorted_issues = sorted(issues)
    if not sorted_issues:
        return {}

    if not os.getenv("GITHUB_API_TOKEN"):
        logger.info("GITHUB_API_TOKEN is not set, skipping the bulk GitHub issue query. "
                    "Issue state will be resolved one issue at a time instead.")
        return {}

    results = {}
    for n_parts in (1, 2, 4):
        if n_parts > len(sorted_issues):
            break
        batches = _split_into_parts(sorted_issues, n_parts)
        try:
            for batch in batches:
                results.update(query_all_issues_in_batch(batch, proxies))
            return results
        except GitHubResourceLimitError:
            pass

    logger.warning("%d issues are still over the limit, leaving them to be resolved "
                   "one at a time",
                   sum(1 for ref in sorted_issues if ref.url not in results))
    return results


def _split_into_parts(items, n_parts):
    """Split items into n_parts of near equal size."""
    part_size, remainder = divmod(len(items), n_parts)
    parts = []
    start = 0
    for index in range(n_parts):
        end = start + part_size + (index < remainder)
        parts.append(items[start:end])
        start = end
    return parts


def query_all_issues_in_batch(refs, proxies):
    """Resolve refs with a single GraphQL request.

    Only GitHubResourceLimitError escapes, since splitting the batch is the one
    failure the caller can act on. Anything else yields an empty dict.
    """
    query, alias_map = _build_query(refs)
    try:
        data = _graphql_post(query, proxies)
    except GitHubResourceLimitError:
        raise
    except Exception as exc:
        logger.error("Bulk GitHub query of %d issues failed: %s", len(refs), exc)
        return {}
    return _parse_response(data, alias_map)


def _group_refs(refs):
    """Group refs by repository, collapsing duplicate issue numbers.

    Returns:
        list: Tuples of (owner, repo, OrderedDict of issue number to URL list).
    """
    groups = OrderedDict()
    for ref in refs:
        if ref.repo_key not in groups:
            groups[ref.repo_key] = (ref.owner, ref.repo, OrderedDict())
        numbers = groups[ref.repo_key][2]
        numbers.setdefault(ref.number, []).append(ref.url)
    return list(groups.values())


def _build_query(refs):
    """Build a single GraphQL query that looks up every ref.

    Each repository and issue gets an alias, since GraphQL allows a field to
    appear once per selection set, and the alias is also the key to read the
    result back out. Only scalar fields are requested: a connection such as
    labels multiplies GitHub's work and shrinks the batch a query can carry.

    Returns:
        tuple: (query string, dict of (repo alias, issue alias) to URL list).
    """
    alias_map = {}
    repo_blocks = []

    for repo_index, (owner, repo, numbers) in enumerate(_group_refs(refs)):
        repo_alias = "r{}".format(repo_index)
        issue_fields = []
        for number, urls in numbers.items():
            issue_alias = "i{}".format(number)
            alias_map[(repo_alias, issue_alias)] = urls
            issue_fields.append(
                "    {alias}: issue(number: {number}) {{ number state stateReason }}".format(
                    alias=issue_alias, number=number))
        repo_blocks.append(
            '  {alias}: repository(owner: "{owner}", name: "{repo}") {{\n{fields}\n  }}'.format(
                alias=repo_alias, owner=owner, repo=repo, fields="\n".join(issue_fields)))

    return "query {{\n{}\n}}".format("\n".join(repo_blocks + [RATE_LIMIT_FIELD])), alias_map


def _parse_response(data, alias_map):
    """Turn an aliased GraphQL response into an issue URL to active state dict.

    Null issues are skipped. GitHub returns null for pull requests and for
    deleted, private, or transferred issues.
    """
    results = {}

    for (repo_alias, issue_alias), urls in alias_map.items():
        repo_data = (data or {}).get(repo_alias)
        if not isinstance(repo_data, dict):
            logger.debug("No data returned for repository alias %s", repo_alias)
            continue

        issue_data = repo_data.get(issue_alias)
        if not isinstance(issue_data, dict):
            logger.debug("No data returned for issue %s, leaving it unresolved", urls[0])
            continue

        # Anything other than an explicit CLOSED counts as active.
        is_active = str(issue_data.get("state") or "").upper() != "CLOSED"
        if not is_active and _is_duplicate(issue_data):
            logger.warning("GitHub issue %s appears to be a duplicate and was closed. "
                           "Consider ignoring related test failures.", urls[0])

        for url in urls:
            results[url] = is_active

    return results


def _is_duplicate(issue_data):
    """Check whether a closed issue was closed as a duplicate.
    """
    return str(issue_data.get("stateReason") or "").upper() == "DUPLICATE"


def _graphql_post(query, proxies=None):
    """POST a GraphQL query, retrying only transient server failures.

    Raises:
        GitHubResourceLimitError: GitHub considered the query too expensive.
        RuntimeError: The request was rate limited, or the response carried no data.
    """
    headers = {
        "Authorization": "Bearer {}".format(os.getenv("GITHUB_API_TOKEN")),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sonic-mgmt-conditional-mark",
    }

    for attempt in range(MAX_RETRIES + 1):
        response = requests.post(GITHUB_GRAPHQL_URL, headers=headers, json={"query": query},
                                 proxies=proxies, timeout=REQUEST_TIMEOUT)

        # Checked before the generic 5xx retry, since retrying a query too big to
        # answer in 10 seconds only burns more of the hourly budget.
        if response.status_code in GITHUB_TIMEOUT_STATUS_CODES:
            raise GitHubResourceLimitError(
                "GitHub timed out on a batch of this size, HTTP {}".format(response.status_code))

        if response.status_code >= 500 and attempt < MAX_RETRIES:
            sleep_secs = min(BACKOFF_FACTOR * (2 ** attempt), MAX_BACKOFF_SECONDS)
            logger.warning("GitHub GraphQL returned %d, retrying in %.1fs (attempt %d/%d)",
                           response.status_code, sleep_secs, attempt + 1, MAX_RETRIES + 1)
            time.sleep(sleep_secs)
            continue

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if _is_rate_limited(response, payload):
            raise RuntimeError("GitHub GraphQL rate limited with HTTP {}".format(response.status_code))

        response.raise_for_status()
        errors = payload.get("errors") or []
        _print_log_rate_limit_status(payload)

        if errors:
            if _is_resource_limit_error(errors):
                raise GitHubResourceLimitError("GitHub reported a resource limit: {}".format(errors))
            # Per-alias failures such as NOT_FOUND arrive alongside good data for the
            # other aliases, so keep whatever GitHub did send.
            logger.warning("GitHub GraphQL reported %d error(s): %s", len(errors), errors)

        data = payload.get("data")
        if not data:
            raise RuntimeError("GitHub GraphQL returned no data, errors: {}".format(errors))
        return data

    raise RuntimeError("GitHub GraphQL request did not complete after {} retries".format(MAX_RETRIES))


def _print_log_rate_limit_status(payload):
    budget = ((payload.get("data") or {}).get("rateLimit")) or {}
    if not budget:
        return
    logger.info("GitHub GraphQL query cost %s point(s), nodeCount %s, %s of %s points "
                "remaining this hour", budget.get("cost"), budget.get("nodeCount"),
                budget.get("remaining"), budget.get("limit"))


def _is_rate_limited(response, payload):
    """True if GitHub rejected the request for exceeding the rate limit."""
    errors = payload.get("errors") or []

    # A successful request may consume the final point, so remaining == 0 only
    # indicates rejection when GitHub also returned an HTTP or GraphQL error.
    if (response.headers.get("X-RateLimit-Remaining") == "0" and
            (response.status_code == 403 or errors)):
        return True

    messages = [payload.get("message")]
    messages.extend(err.get("message") for err in errors if isinstance(err, dict))
    return any(SECONDARY_RATE_LIMIT_RE.search(str(message))
               for message in messages if message)


def _is_resource_limit_error(errors):
    """Check whether GitHub rejected the query for being too expensive.    """
    for err in errors:
        if str(err.get("type") or "").upper() == GITHUB_RESOURCE_LIMIT_ERROR_TYPE:
            return True
        if GITHUB_RESOURCE_LIMIT_MESSAGE in str(err.get("message") or "").lower():
            return True
    return False
