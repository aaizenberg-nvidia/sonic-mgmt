"""Helpers for resolving NBU-SW Gerrit change numbers into refs that can be fetched.

Shared by every ``--<component>_change`` option that accepts a numeric Gerrit change
number (e.g. ``--perf_sys_sdk_change``, ``--sonic_mgmt_change``), so the
``git ls-remote`` + latest-patchset-selection logic lives in one place instead of being
duplicated per component.
"""
import json
import logging
import re
import subprocess

import requests

from devts.infra.tools.exceptions.test_issue import TestIssue

logger = logging.getLogger()

GERRIT_REST_API_URL = "https://git-nbu-sw.nvidia.com/r/a"
GERRIT_CHANGE_STATUS_MERGED = "MERGED"

# Any `git` invocation that talks to a remote over SSH (fetch, ls-remote) blocks
# indefinitely on a first-contact host-key confirmation prompt when the running
# host/container has no matching ~/.ssh/known_hosts entry yet - the default
# `StrictHostKeyChecking=ask` waits on stdin, which is never connected to a terminal
# in an automated MARS/CI run, so the process hangs until an external test timeout
# SIGKILLs it. `accept-new` auto-trusts a host on first contact (still rejecting a
# later key change on an already-known host is still rejected), and
# `BatchMode=yes` guarantees no other prompt (e.g. a passphrase) can block either.
# Scoped via "-c core.sshCommand=..." to the single invocation, not persisted anywhere.
NONINTERACTIVE_GIT_SSH_ARGS = [
    "-c", "core.sshCommand=ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
]


def get_latest_patchset_ref(repo, change_number, cwd=None, extra_git_config=None):
    """Resolve the ref of the latest patchset for a Gerrit change number.

    Runs ``git ls-remote <repo> refs/changes/*/<change_number>/*`` and returns the ref with
    the highest patchset number.

    Args:
        repo (str): Repository to query - either a full URL (with or without embedded
            credentials) or a configured remote name such as ``'origin'``, in which case
            ``cwd`` must point at an existing clone with that remote configured.
        change_number (str | int): Gerrit change number.
        cwd (str, optional): Working directory to run ``git`` from. Required when ``repo``
            is a remote name rather than a full URL.
        extra_git_config (list[str], optional): Extra ``"key=value"`` pairs applied as
            ``-c key=value`` before the ``ls-remote`` subcommand, e.g. to supply a
            credential helper so an HTTPS `repo` URL doesn't need embedded credentials.

    Returns:
        str: The ref of the latest patchset, e.g. ``'refs/changes/42/347942/3'``.

    Raises:
        RuntimeError: If ``git ls-remote`` fails.
        TestIssue: If the change has no patch sets on the remote.
    """
    change_number = str(change_number)
    if not change_number.isdigit():
        logger.error("Invalid Gerrit change number: %s (expected digits only)", change_number)
        raise TestIssue(f"Invalid Gerrit change number: {change_number!r} (expected digits only)")
    sanitized_repo = re.sub(r'://[^@]+@', '://***:***@', repo)
    ref_pattern = f"refs/changes/*/{change_number}/*"
    # When `repo` is a remote name (e.g. "origin"), git must open the local clone at `cwd`
    # to resolve it, which fails with "dubious ownership" on hosts/containers where that
    # directory's owner UID isn't in the local user database. Scope trust to this single
    # invocation instead of requiring a per-host git config fix.
    git_prefix = ["git", "-c", f"safe.directory={cwd}"] if cwd else ["git"]
    git_prefix = git_prefix + NONINTERACTIVE_GIT_SSH_ARGS
    for config in extra_git_config or []:
        git_prefix = git_prefix + ["-c", config]
    git_ls_remote_command = git_prefix + ["ls-remote", repo, ref_pattern]
    # Log a credential-sanitized copy: `repo` may embed a token/password in its URL.
    sanitized_git_ls_remote_command = git_prefix + ["ls-remote", sanitized_repo, ref_pattern]
    logger.info("About to execute command: %s", sanitized_git_ls_remote_command)
    try:
        result = subprocess.run(git_ls_remote_command, cwd=cwd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        sanitized_stderr = re.sub(r'://[^@]+@', '://***:***@', e.stderr or '')
        sanitized_stdout = re.sub(r'://[^@]+@', '://***:***@', e.stdout or '')
        raise RuntimeError(
            f'git ls-remote failed for change {change_number} at {sanitized_repo} with exit code '
            f'{e.returncode}. stderr: {sanitized_stderr.strip()}. stdout: {sanitized_stdout.strip()}'
        ) from None
    patchset_ref_pattern = re.compile(rf"^refs/changes/\d+/{change_number}/(\d+)$")
    patchsets = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            patchset_ref_match = patchset_ref_pattern.match(parts[1])
            if patchset_ref_match:
                patchsets[int(patchset_ref_match.group(1))] = parts[1]
    if not patchsets:
        raise TestIssue(f"No patch sets found for Gerrit change {change_number} at {sanitized_repo}")
    latest_patchset = max(patchsets)
    latest_ref = patchsets[latest_patchset]
    logger.info(f"Resolved Gerrit change {change_number} to latest patchset {latest_patchset}: {latest_ref}")
    return latest_ref


def get_change_status(change_number, username, password):
    """Return a Gerrit change's status via the REST API.

    Args:
        change_number (str | int): Gerrit change number.
        username (str): Gerrit username for HTTP basic auth.
        password (str): Gerrit HTTP password / API token.

    Returns:
        str: The change status, e.g. ``'NEW'``, ``'MERGED'`` or ``'ABANDONED'``.

    Raises:
        requests.HTTPError: If the request fails, e.g. the change does not exist.
    """
    url = f"{GERRIT_REST_API_URL}/changes/{change_number}"
    logger.info("Querying Gerrit change status: %s", url)
    response = requests.get(url, auth=(username, password), timeout=30)
    response.raise_for_status()
    # Gerrit prefixes every JSON response with a ")]}'" line to prevent cross-site script inclusion.
    status = json.loads(response.text.split("\n", 1)[1])["status"]
    logger.info(f"Gerrit change {change_number} status: {status}")
    return status
