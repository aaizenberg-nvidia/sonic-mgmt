#!/usr/bin/env python3
import json
import os
import yaml
import logging
import re
import sys
import subprocess
logger = logging.getLogger()
# Check and install python-gerrit-api if needed
try:
    import gerrit
    logger.info("python-gerrit-api is already installed")
except ImportError:
    logger.info("python-gerrit-api not found, installing...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-gerrit-api"])
    logger.info("Successfully installed python-gerrit-api")

path = os.path.abspath(__file__)
sonic_mgmt_path = path.split('/ngts/')[0]
sys.path.append(sonic_mgmt_path)
if "/devts" not in sys.path:
    sys.path.append("/devts")

from datetime import datetime, timedelta
from gerrit.base import GerritClient
from ngts.constants.constants import Sonic_Cache, MarsConstants
from devts.infra.tools.redmine.redmine_api import get_issues_active_status

logger = logging.getLogger()


LA_DYNAMIC_LOGANALYZER = "tests/common/plugins/loganalyzer_dynamic_errors_ignore/dynamic_loganalyzer_ignores.yaml"
LA_DYNAMIC_IGNORE_PREFIX = "tests/common/plugins/loganalyzer_dynamic_errors_ignore/"
LA_DYNAMIC_IGNORE_PATH = os.path.join(MarsConstants.SONIC_MGMT_DIR, LA_DYNAMIC_IGNORE_PREFIX)
CONDITIONAL_MARK_PREFIX = "tests/common/plugins/conditional_mark/"
DEFAULT_CONDITIONS_FILE = os.path.join(MarsConstants.SONIC_MGMT_DIR + CONDITIONAL_MARK_PREFIX, "tests_mark_conditions*.yaml")
LA_DYNAMIC_FILES = [
    "tests/common/plugins/loganalyzer_dynamic_errors_ignore/dynamic_loganalyzer_ignores.yaml",
    "tests/common/plugins/loganalyzer_dynamic_errors_ignore/dynamic_loganalyzer_ignores_nvos.yaml"
]
CONDITIONAL_MARK_FILES = [
    "tests_mark_conditions.yaml",
    "tests_mark_conditions_acl.yaml",
    "tests_mark_conditions_cl_internal.yaml",
    "tests_mark_conditions_drop_packets.yaml",
    "tests_mark_conditions_nvidia_internal.yaml",
    "tests_mark_conditions_nvos_internal.yaml",
    "tests_mark_conditions_platform_tests.yaml",
    "tests_mark_conditions_skip_traffic_test.yaml",
]

DAYS_THRESHOLD = 7


def get_all_branches(client):
    """
    Get all branches from gerrit that match develop prefix.
    """
    all_branches = []
    skip = 0
    limit = 50
    branch_pattern = re.compile(r"^(?:develop(?:-\d{6})?|nvos_ver-\d{2}-\d{2}-\d{4})$")
    # pull branches from gerrit in batches of 50
    while True:
        branches = client.projects.get(Sonic_Cache.PROJECT_NAME).branches.list(limit=limit, skip=skip)
        if not branches:
            break
        matching_branches = [b['ref'].replace('refs/heads/', '') for b in branches if branch_pattern.match(b['ref'].replace('refs/heads/', ''))]
        all_branches.extend(matching_branches)
        skip += len(branches)

    return all_branches


def get_active_branches(client, all_branches):
    """
    Get all active develop branches from gerrit.
    """
    active_branches = []
    since_date = (datetime.utcnow() - timedelta(days=DAYS_THRESHOLD)).strftime('%Y-%m-%d')

    for branch_name in all_branches:
        query = f"project:{Sonic_Cache.PROJECT_NAME} branch:{branch_name} mergedafter:{since_date}"
        if client.changes.search(query):
            active_branches.append(branch_name)

    return active_branches


def extract_redmine_issues_from_branch(client, branch):
    """
    Extract all Redmine issues from a specific branch by reading the YAML files.
    Returns a set of unique issue IDs from both LA and conditional files.
    """
    project = client.projects.get(Sonic_Cache.PROJECT_NAME)
    branch_obj = project.branches.get(branch)

    all_issues = set()

    # Extract issues from LA dynamic files
    for la_file in LA_DYNAMIC_FILES:
        try:
            la_content = branch_obj.get_file_content(la_file, decode=True)
            la_data = yaml.safe_load(la_content)
            la_json = json.dumps(la_data)
            branch_la_issues = re.findall(r"\"Redmine\":\s*\[(\d+)\]", la_json)
            all_issues.update(branch_la_issues)
        except Exception as e:
            logger.error(f"Skipped missing or invalid file {la_file} in branch {branch}: {e}")

    # Extract issues from conditional mark files
    for file_path in CONDITIONAL_MARK_FILES:
        try:
            file_content = branch_obj.get_file_content(CONDITIONAL_MARK_PREFIX + file_path, decode=True)
            file_data = yaml.safe_load(file_content)
            file_json = json.dumps(file_data)
            branch_conditional_issues = re.findall(r"https:\/\/redmine\.mellanox\.com\/issues\/(\d+)", file_json)
            all_issues.update(branch_conditional_issues)
        except Exception as e:
            logger.error(f"Skipped missing or invalid file {file_path} in branch {branch}: {e}")

    return all_issues


def collect_all_unique_issues(client, active_branches):
    """
    Collect all unique Redmine issues from all active branches.
    Returns a set of all unique issue IDs.
    """
    all_issues = set()

    logger.info(f"Collecting unique Redmine issues from {len(active_branches)} active branches")

    for branch in active_branches:
        logger.info(f"Processing branch: {branch}")
        branch_issues = extract_redmine_issues_from_branch(client, branch)
        all_issues.update(branch_issues)

    logger.info(f"Found {len(all_issues)} unique Redmine issues across all branches")

    return all_issues


def get_status_for_all_issues(all_issues):
    """
    Get the status for all unique issues in a single API call.
    Returns a dictionary mapping issue IDs to their status.
    """
    logger.info(f"Getting status for {len(all_issues)} unique Redmine issues")
    issues_status_dict = get_issues_active_status(list(all_issues))
    return issues_status_dict


def get_service_env_vars():
    """
    Get the service account environment variables from the environment file.
    """
    HOST_SERVER_IP = os.getenv("HOST_SERVER_IP", "10.215.19.80")
    SERVICE_ACCOUNT_USER = os.getenv("SERVICE_ACCOUNT_USER", "svc-nbu-sws-sonic")
    SERVICE_ACCOUNT_PASSWORD = os.getenv("TEST_SERVER_PASSWORD")
    if not SERVICE_ACCOUNT_PASSWORD or not SERVICE_ACCOUNT_USER:
        logger.error("SERVICE_ACCOUNT_PASSWORD environment variable is not set")
        raise ValueError("SERVICE_ACCOUNT_PASSWORD environment variable is required")
    return HOST_SERVER_IP, SERVICE_ACCOUNT_USER, SERVICE_ACCOUNT_PASSWORD


def copy_cache_to_host_server(combined_issues_status, temp_file, remote_temp_file):
    """
    Copy the redmine cache to the host server.
    """
    HOST_SERVER_IP, SERVICE_ACCOUNT_USER, SERVICE_ACCOUNT_PASSWORD = get_service_env_vars()
    try:
        with open(temp_file, 'w') as f:
            json.dump(combined_issues_status, f, indent=2)

        scp_cmd = [
            "sshpass", "-p", SERVICE_ACCOUNT_PASSWORD,
            "scp",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            temp_file,
            f"{SERVICE_ACCOUNT_USER}@{HOST_SERVER_IP}:{remote_temp_file}"
        ]
        logger.info(f"Copying cache from container to /tmp on host server {HOST_SERVER_IP}")
        result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            logger.error(f"Failed to copy cache to host server /tmp: {result.stderr}")
            raise RuntimeError(f"SCP failed: {result.stderr}")
    except Exception as e:
        logger.error(f"Error in copy_cache_to_host_server: {str(e)}")
        raise


def copy_cache_to_target_path(remote_temp_file):
    """
    Copy the redmine cache to the target path.
    """
    HOST_SERVER_IP, SERVICE_ACCOUNT_USER, SERVICE_ACCOUNT_PASSWORD = get_service_env_vars()
    remote_temp_file = "/tmp/redmine_cache_temp.json"
    ssh_cmd = [
        "sshpass", "-p", SERVICE_ACCOUNT_PASSWORD,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"{SERVICE_ACCOUNT_USER}@{HOST_SERVER_IP}",
        f"sudo cp {remote_temp_file} {Sonic_Cache.REDMINE_ISSUES_STATUS_CACHE} && sudo chmod 644 {Sonic_Cache.REDMINE_ISSUES_STATUS_CACHE} && rm {remote_temp_file}"
    ]
    logger.info(f"Service account copying from /tmp to {Sonic_Cache.REDMINE_ISSUES_STATUS_CACHE}")
    result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        logger.error(f"Failed to copy from /tmp to final location: {result.stderr}")
        raise RuntimeError(f"SSH command failed: {result.stderr}")

    logger.info(f"Successfully wrote redmine cache to {Sonic_Cache.REDMINE_ISSUES_STATUS_CACHE} on host server")


def write_redmine_cache(combined_issues_status):
    """
    Write the redmine cache to the target server using service account with password authentication.
    This function runs from within a container and writes to a host server.
    """
    temp_file = "/tmp/redmine_cache_temp.json"
    remote_temp_file = "/tmp/redmine_cache_temp.json"
    try:
        copy_cache_to_host_server(combined_issues_status, temp_file, remote_temp_file)
        copy_cache_to_target_path(remote_temp_file)
    except subprocess.TimeoutExpired:
        logger.error("Timeout while copying cache to host server")
        raise
    except Exception as e:
        logger.error(f"Error writing cache to host server: {str(e)}")
        raise
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)
        logger.info("Successfully wrote redmine cache to target server")


def get_all_active_branches_issues_status():
    """
    Generate the dictionary containing the redmine issues status for all active branches.
    """
    username = Sonic_Cache.gerrit_username
    token = Sonic_Cache.gerrit_api_token
    client = GerritClient(base_url=Sonic_Cache.GERRIT_API_URL, username=username, password=token)
    all_branches = get_all_branches(client)
    active_branches = get_active_branches(client, all_branches)
    all_issues = collect_all_unique_issues(client, active_branches)
    all_issues_status = get_status_for_all_issues(all_issues)
    return all_issues_status


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    try:
        # Run the main test function
        combined_issues_status = get_all_active_branches_issues_status()
        if not combined_issues_status:
            logger.warning("No redmine issues found - this might indicate API issues during maintenance")
            logger.warning("Skipping cache update to avoid overwriting with empty data")
        else:
            write_redmine_cache(combined_issues_status)
            logger.info("Successfully generated Redmine cache")
            logger.info(f"Number of issues processed: {len(combined_issues_status)}")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Failed to generate Redmine cache: {str(e)}", exc_info=True)
        sys.exit(1)
