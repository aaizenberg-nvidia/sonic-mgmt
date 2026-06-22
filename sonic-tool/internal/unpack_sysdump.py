#!/usr/bin/env python3

"""
Python 3 compatible standalone version of unpack_sysdump.py
This script can unpack sysdump files and combine log files
Supports both Redmine and NVBugs issue attachments
"""

import os
import sys
import re
import json
import tempfile
import shutil
import tarfile
import gzip
import argparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from redminelib import Redmine
    from redminelib.exceptions import ResourceNotFoundError
    REDMINE_AVAILABLE = True
except ImportError as e:
    REDMINE_AVAILABLE = False
    Redmine = None
    ResourceNotFoundError = Exception

REDMINE_URL = 'https://redmine-api.nvidia.com'
NVBUGS_API_BASE = 'https://prod.api.nvidia.com/int/nvbugs/api'
NVBUGS_API_TIMEOUT = 60
# Repo root (…/sonic-mgmt) so the shared ngts VaultClient can be imported.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REDMINE_TOKEN_ENV = 'REDMINE_API_TOKEN'
NVBUGS_TOKEN_ENV = 'NVBUG_API_TOKEN'

LOG_FILE_SEQUENCE_ID_IDX = 1

_SECRETS_FETCHED = False
_REDMINE_KEY = None
_NVBUGS_TOKEN = None


def vault_login():
    """
    Fetch Redmine/NVBugs secrets from Vault into the environment using the shared
    ngts VaultClient (vault agent + AppRole auth).
    Raises:
        Exception: If Vault authentication or secret fetching fails
    """
    global _SECRETS_FETCHED
    if _SECRETS_FETCHED:
        return

    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)

    try:
        from ngts.nvos_tools.infra.VaultClient import VaultClient
    except ImportError as err:
        raise Exception(
            "Could not import ngts VaultClient (expected under %s): %s" % (_REPO_ROOT, err))

    VaultClient.fetch_and_export_secrets()
    _SECRETS_FETCHED = True


def get_redmine_key():
    """
    Load the Redmine API token from Vault
    Returns:
        str: Redmine API token
    """
    global _REDMINE_KEY
    if _REDMINE_KEY is None:
        vault_login()
        _REDMINE_KEY = os.environ.get(REDMINE_TOKEN_ENV)
        if not _REDMINE_KEY:
            raise Exception(
                "%s was not provided by Vault. Check the MARS secrets template and your Vault access."
                % REDMINE_TOKEN_ENV)
    return _REDMINE_KEY


def get_nvbugs_token():
    """
    Load the NVBugs API token from Vault
    Returns:
        str: NVBugs API token
    """
    global _NVBUGS_TOKEN
    if _NVBUGS_TOKEN is None:
        vault_login()
        _NVBUGS_TOKEN = os.environ.get(NVBUGS_TOKEN_ENV)
        if not _NVBUGS_TOKEN:
            raise Exception(
                "%s was not provided by Vault. Check the MARS secrets template and your Vault access."
                % NVBUGS_TOKEN_ENV)
    return _NVBUGS_TOKEN


def get_redmine_connection():
    """
    Return a Redmine connection object.
    This requires the python-redmine package to be installed.
    Returns:
        Redmine: Connection object to Redmine API
    """
    if not REDMINE_AVAILABLE:
        print("ERROR: python-redmine package is not installed.")
        print("Please install it using: sudo pip3 install python-redmine")
        sys.exit(1)

    return Redmine(REDMINE_URL, key=get_redmine_key())


def download_redmine_attachments(issue_num, output_dir):
    """
    Download all attachments from a Redmine issue.
    Args:
        issue_num: Redmine issue number
        output_dir: Directory to save attachments to
    """
    redmine_key = get_redmine_key()
    rm = get_redmine_connection()

    try:
        issue = rm.issue.get(int(issue_num))
    except ResourceNotFoundError as err:
        raise Exception("%s, bug %s does not exist in Redmine" % (err, issue_num))

    for attach in issue['attachments']:
        attachment_id = attach['id']
        filename = os.path.basename(attach['filename'] or '')
        dl = attach['content_url']

        if not attachment_id or not filename:
            print("Warning: Skipping attachment with missing ID or filename: %s" % attach)
            continue

        # Insert the globally unique attachment ID before the file extension so
        # attachments that share the same filename don't overwrite each other.
        if filename.endswith('.tar.gz'):
            unique_name = '%s_%s.tar.gz' % (filename[:-len('.tar.gz')], attachment_id)
        else:
            root, ext = os.path.splitext(filename)
            unique_name = '%s_%s%s' % (root, attachment_id, ext)

        dest_path = os.path.join(output_dir, unique_name)

        try:
            _download_url_to_file(
                dl, dest_path,
                headers={'X-Redmine-API-Key': redmine_key},
            )
        except Exception as e:
            print("Warning: Failed to download attachment %s: %s" % (unique_name, e))
            continue

    if issue['attachments']:
        print("\nIssue %d attachments were downloaded to %s" % (issue_num, output_dir))


def _nvbugs_auth_headers():
    """
    Return HTTP headers for NVBugs REST API requests.
    Returns:
        dict: Authorization headers using the shared NVBugs token
    """
    return {
        'Authorization': 'Bearer %s' % get_nvbugs_token(),
        'Accept': 'application/json',
    }


def _nvbugs_get_json(url):
    """
    Perform an authenticated GET request to the NVBugs API and parse JSON.
    Args:
        url: Full NVBugs API URL
    Returns:
        Parsed JSON response body
    Raises:
        Exception: On HTTP or network errors
    """
    request = Request(url, headers=_nvbugs_auth_headers())
    try:
        with urlopen(request, timeout=NVBUGS_API_TIMEOUT) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as err:
        if err.code in (401, 403):
            raise Exception(
                "NVBugs API request failed (HTTP %s). "
                "The NVBugs token from Vault may be expired or invalid, or you may lack access "
                "to this bug."
                % err.code)
        raise Exception("NVBugs API request failed (%s): %s" % (err.code, err.reason))
    except URLError as err:
        raise Exception("NVBugs API request failed: %s" % err.reason)
    except json.JSONDecodeError as err:
        raise Exception("NVBugs API returned invalid JSON: %s" % err)


def _download_url_to_file(url, dest_path, headers=None, timeout=NVBUGS_API_TIMEOUT):
    """
    Download a URL to a local file.
    Args:
        url: URL to download
        dest_path: Local path to write the file to
        headers: Optional request headers
        timeout: Request timeout in seconds
    Raises:
        Exception: On HTTP or network errors
    """
    request = Request(url, headers=headers or {})

    try:
        with urlopen(request, timeout=timeout) as response:
            with open(dest_path, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
    except URLError as err:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        if isinstance(err, HTTPError):
            raise Exception("Download failed (HTTP %s): %s" % (err.code, err.reason))
        raise Exception("Download failed: %s" % err.reason)


def _parse_nvbugs_attachment_list(payload):
    """
    Normalize NVBugs attachment list responses.
    DetailsUsingBugID returns a JSON array; some NVBugs APIs wrap data in ReturnValue.
    Args:
        payload: Parsed JSON from DetailsUsingBugID
    Returns:
        list: Attachment metadata dicts
    """
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        if payload.get('IsSuccess') is False:
            error_message = payload.get('ErrorMessage') or 'unknown NVBugs API error'
            raise Exception(error_message)

        return_value = payload.get('ReturnValue')
        if isinstance(return_value, list):
            return return_value

    raise Exception("Unexpected NVBugs attachment response format")


def get_nvbugs_attachments(bug_id):
    """
    Fetch attachment metadata for an NVBugs bug.
    Args:
        bug_id: NVBugs bug ID
    Returns:
        list: Attachment metadata dicts (FileName, AttachmentGUID, etc.)
    """
    url = '%s/Attachment/DetailsUsingBugID/%d' % (NVBUGS_API_BASE, int(bug_id))
    payload = _nvbugs_get_json(url)
    return _parse_nvbugs_attachment_list(payload)


def download_nvbugs_attachments(bug_id, output_dir):
    """
    Download all attachments from an NVBugs bug.
    Args:
        bug_id: NVBugs bug ID
        output_dir: Directory to save attachments to
    """
    attachments = get_nvbugs_attachments(bug_id)

    if not attachments:
        raise Exception("Bug %s has no attachments in NVBugs" % bug_id)

    for attach in attachments:
        guid = attach.get('AttachmentGUID')
        raw_name = attach.get('FileName')
        filename = os.path.basename(raw_name or '')

        if not guid or not filename:
            print("Warning: Skipping attachment with missing GUID or filename: %s" % attach)
            continue

        # Insert the globally unique AttachmentGUID before the file extension so
        # attachments that share the same FileName don't overwrite each other.
        if filename.endswith('.tar.gz'):
            unique_name = '%s_%s.tar.gz' % (filename[:-len('.tar.gz')], guid)
        else:
            root, ext = os.path.splitext(filename)
            unique_name = '%s_%s%s' % (root, guid, ext)

        dest_path = os.path.join(output_dir, unique_name)
        download_url = '%s/Attachment/DownloadAttachment?attachmentGUID=%s' % (
            NVBUGS_API_BASE, guid)

        try:
            _download_url_to_file(download_url, dest_path, headers=_nvbugs_auth_headers())
        except Exception as e:
            print("Warning: Failed to download attachment %s: %s" % (unique_name, e))
            continue

    if attachments:
        print("\nBug %s attachments were downloaded to %s" % (bug_id, output_dir))


def get_sysdumps_and_messages_dirs(output_dir):
    """
    Parse directory to find sysdump files and message directories.
    Args:
        output_dir: Directory to search in
    Returns:
        tuple: (list of sysdump paths, list of message directory paths)
    """
    sysdumps = []
    messages_dirs = []
    files = os.listdir("./")

    for file_name in files:
        if file_name.endswith(".tgz"):
            # should match sysdump_10_210_24_156_MSN2700-Panther.tgz / sysdump-tarantula108-20160223-110202.tgz
            # / sysdump-switch-645c6a-20150209-080412.tgz / sysdump-ptr-vcl-02-20160505-144036.tgz
            if re.match(r'^sysdump\S+\.tgz$', file_name):
                sysdumps.append(os.path.abspath(file_name))

            # should match 10_224_13_185_sysdump.tgz (optionally with a GUID suffix)
            elif re.match(r'^(\d+|_)+sysdump\S*\.tgz$', file_name):
                sysdumps.append(os.path.abspath(file_name))

            # should match 10.224.22.161.tgz (optionally with a GUID suffix)
            elif re.match(r'^(\d+|\.)+\S*tgz$', file_name):
                unzipped_dir = file_name.strip(".tgz")
                os.mkdir(unzipped_dir)
                shutil.move(file_name, "%s/%s" % (unzipped_dir, file_name))
                os.chdir(unzipped_dir)

                tar = tarfile.open(file_name)
                tar.extractall()
                tar.close()

                for inner_file in os.listdir('./'):
                    if re.match('^sysdump', inner_file):
                        sysdumps.append(os.path.abspath(inner_file))
                    if re.match('^messages', inner_file):
                        dir_path = os.path.dirname(os.path.realpath(inner_file))
                        if dir_path not in messages_dirs:
                            messages_dirs.append(dir_path)

                # delete original zip file
                os.remove(file_name)
                os.chdir(output_dir)

            else:
                pass

        elif file_name.endswith("tar.gz"):
            # should match SONiC dump files
            sysdumps.append(os.path.abspath(file_name))

    return sysdumps, messages_dirs


def atoi(text):
    """
    Convert text to integer if it's a digit, otherwise return 0.
    """
    if text.isdigit():
        return int(text)
    else:
        return 0


def get_sequence_id(file_name):
    """
    Extract sequence ID from log file name.
    Args:
        file_name: Name of the log file
    Returns:
        int: Sequence ID
    """
    name_parts = re.split(r'(\d+)', file_name)

    if len(name_parts) == 1:
        # handle files without sequence id e.g. 'syslog.gz'.
        # split a string by digit when there is no digit in the string will return list that contains the original string, name_parts will be [syslog.gz]
        sequence_id = 0
    elif len(name_parts) > 1:
        # handle files with sequence id e.g. 'syslog.1.gz', name_parts will be ['syslog.', '1', '.gz']
        sequence_id = atoi(name_parts[LOG_FILE_SEQUENCE_ID_IDX])
    else:
        raise Exception("Failed to parse sequence id of file %s" % (file_name))

    return sequence_id


def combine_messages(messages_dir):
    """
    Combine all log files in a directory into single files per log type.
    Args:
        messages_dir: Directory containing log files
    """
    log_files_prefixes = ['teamd', 'syslog', 'zebra', 'telemetry', 'cron', 'bgpd', 'auth',
                          'swss.rec', 'sairedis.rec', 'responsepublisher', 'messages',
                          'debug', 'web_access_log', 'opensm_infiniband-default']
    os.chdir(messages_dir)

    for prefix in log_files_prefixes:
        log_files_combined = prefix + '_combined.txt'
        log_files = []
        for file_name in os.listdir('./'):
            if file_name.startswith(prefix):
                log_files.append(file_name)
        combine_files(log_files, log_files_combined)


def combine_files(files_to_combine, dst_file):
    """
    Combine multiple files into a single destination file.
    Args:
        files_to_combine: List of files to combine
        dst_file: Destination file path
    """
    if files_to_combine == []:
        return

    dst_fd = open(dst_file, 'a')

    files_to_combine.sort(key=get_sequence_id, reverse=True)

    for file_name in files_to_combine:
        if file_name.endswith(".gz"):
            src_fd = gzip.open(file_name, 'rt')
        else:
            src_fd = open(file_name, 'r')
        for line in src_fd.readlines():
            dst_fd.write(line)
        src_fd.close()

    dst_fd.close()
    print("%s" % (dst_file))


def extract_sysdumps_combine_messages(sysdumps, messages_dirs, output_dir, issue_num=0,
                                      issue_source='NVBugs'):
    """
    Extract sysdump files and combine their log files.
    Args:
        sysdumps: List of sysdump file paths
        messages_dirs: List of message directory paths
        output_dir: Output directory
        issue_num: Issue number (optional, default: 0)
        issue_source: Tracker name for messages (Redmine or NVBugs)
    """
    if sysdumps == [] and messages_dirs == []:
        shutil.rmtree(output_dir)
        print("No sysdumps found in %s issue %d" % (issue_source, issue_num))

    for sysdump_path in sysdumps:
        try:
            sysdump_dir = os.path.dirname(os.path.realpath(sysdump_path))
            sysdump_file = os.path.basename(sysdump_path)
            tmp_dir = tempfile.mkdtemp()

            shutil.move(sysdump_path, "%s/%s" % (tmp_dir, sysdump_file))
            os.chdir(tmp_dir)

            # extract sysdump file
            tar = tarfile.open(sysdump_file)
            try:
                # Validate tar members to prevent path traversal attacks
                for member in tar.getmembers():
                    # Check for absolute paths or path traversal attempts
                    if os.path.isabs(member.name) or ".." in member.name:
                        raise Exception("Unsafe tar member detected: %s. Possible path traversal attempt." % member.name)

                # Safe to extract after validation
                tar.extractall()
            except OSError:
                print("\nUnpacking %s requires root permissions. Please rerun with user root or with sudo privilege" % (sysdump_file))
                continue
            finally:
                tar.close()

            # delete original sysdump file
            os.remove(sysdump_file)

            # remove symbolic links
            os.system('find -type l -delete')

            dir_list = os.listdir('./')
            if len(dir_list) > 0:
                unzipped_sysdump = dir_list[0]
                unzipped_sysdump_path = os.path.abspath(unzipped_sysdump)
            else:
                raise Exception("Can not get path to unzipped sysdump")

            # determine final destination, avoiding collisions when multiple
            # sysdumps share the same internal directory name
            final_dir = os.path.join(sysdump_dir, unzipped_sysdump)
            if os.path.exists(final_dir):
                suffix = 1
                while os.path.exists("%s_%d" % (final_dir, suffix)):
                    suffix += 1
                final_dir = "%s_%d" % (final_dir, suffix)

            # create file messages_combine.txt that contain all messages files
            log_folder = ''
            if os.path.isdir(os.path.join(unzipped_sysdump_path, 'log')):
                log_folder = '/log'

            print("\nThe following files created under: %s%s" % (final_dir, log_folder))
            combine_messages(unzipped_sysdump_path + log_folder)

            shutil.move(unzipped_sysdump_path, final_dir)
            os.chdir(output_dir)
            shutil.rmtree(tmp_dir)
        except Exception as error:
            print('Exception received [%s] while trying to handle file [%s]' % (error, sysdump_path))
            continue

    for messages_dir in messages_dirs:
        print("\nThe following files created under %s" % (messages_dir))
        combine_messages(messages_dir)


def create_output_dir(suffix, source='nvbugs'):
    """
    Create output directory for unpacked files.
    Args:
        suffix: Suffix for directory name (issue number or sysdump name)
        source: 'nvbugs', 'redmine', or 'local' for a local sysdump file
    Returns:
        str: Path to created output directory
    """
    if source == 'redmine':
        output_dir = "/tmp/redmine_%s_attachments" % (suffix)
    elif source == 'nvbugs':
        output_dir = "/tmp/nvbugs_%s_attachments" % (suffix)
    else:
        basename = os.path.basename(suffix)
        sysdump = basename.split('.')[0]
        output_dir = "/tmp/%s/" % (sysdump)

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.mkdir(output_dir)
    return output_dir


def get_args():
    """
    Parse command line arguments.
    Returns:
        argparse.Namespace: Parsed arguments
    """
    parser = argparse.ArgumentParser(
        description="Use option --redmine_issue_number (-r) to download attachments from "
                    "Redmine issue and extract all sysdump files. For each sysdump, "
                    "combine all log files into a single file.\n"
                    "Use option --nvbugs_issue_number (-n) to download attachments from "
                    "NVBugs bug and extract all sysdump files. For each sysdump, "
                    "combine all log files into a single file.\n"
                    "Use option --sysdump (-s) to extract given sysdump file and combine "
                    "all log files into a single file.\n"
                    "Use option --dir (-d) to create a single file from all log "
                    "files that exist in the given extracted sysdump (log folder is expected).\n",
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('-r', '--redmine_issue_number', type=int, help='Redmine issue number', required=False)
    parser.add_argument('-n', '--nvbugs_issue_number', type=int, help='NVBugs bug ID', required=False)
    parser.add_argument('-s', '--sysdump', type=str, help='Path to sysdump file', required=False)
    parser.add_argument('-d', '--dir', type=str, help='Path to extracted sysdump directory', required=False)

    options = parser.parse_args()
    if not options.redmine_issue_number and not options.nvbugs_issue_number \
            and not options.dir and not options.sysdump:
        parser.error("One of -r, -n, -s, or -d must be given")
    return options


if __name__ == '__main__':
    try:
        # parse user arguments
        options = get_args()

        # authenticate with Vault only when remote tracker download is requested
        if options.redmine_issue_number is not None or options.nvbugs_issue_number is not None:
            vault_login()

        if (options.sysdump is not None):
            # option --sysdump used
            path_to_sysdump = os.path.abspath(options.sysdump)
            output_dir = create_output_dir(path_to_sysdump, source='local')
            shutil.copy(path_to_sysdump, output_dir)
            os.chdir(output_dir)

            sysdump_file = os.path.basename(path_to_sysdump)
            sysdumps = [os.path.join(output_dir, sysdump_file)]

            # extract sysdump file and create combined log files
            extract_sysdumps_combine_messages(sysdumps, [], output_dir)

        elif (options.dir is not None):
            # option --dir used
            messages_dir = os.path.abspath(options.dir)

            # create combined messages file
            extract_sysdumps_combine_messages([], [messages_dir], [])

        elif (options.redmine_issue_number is not None):
            # option --redmine_issue_number used
            output_dir = create_output_dir(options.redmine_issue_number, source='redmine')
            os.chdir(output_dir)

            # download attachments
            download_redmine_attachments(options.redmine_issue_number, output_dir)

            # parse attachments to get path of all sysdumps files and independent folders that hold messages files
            sysdumps, messages_dirs = get_sysdumps_and_messages_dirs(output_dir)

            # extract sysdump files and create combined log files
            extract_sysdumps_combine_messages(sysdumps, messages_dirs, output_dir,
                                              options.redmine_issue_number, 'Redmine')

        elif (options.nvbugs_issue_number is not None):
            # option --nvbugs_issue_number used
            output_dir = create_output_dir(options.nvbugs_issue_number, source='nvbugs')
            os.chdir(output_dir)

            # download attachments
            download_nvbugs_attachments(options.nvbugs_issue_number, output_dir)

            # parse attachments to get path of all sysdumps files and independent folders that hold messages files
            sysdumps, messages_dirs = get_sysdumps_and_messages_dirs(output_dir)

            # extract sysdump files and create combined log files
            extract_sysdumps_combine_messages(sysdumps, messages_dirs, output_dir,
                                              options.nvbugs_issue_number, 'NVBugs')

    except Exception as e:
        print("%s" % e)
        sys.exit(1)
