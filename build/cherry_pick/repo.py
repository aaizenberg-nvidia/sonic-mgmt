from os import path
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from logger import logger
import enum

_CHRRY_PICK_EMPTY = "The previous cherry-pick is now empty, possibly due to conflict resolution"
_CHERRY_PICK_MERGE = "is a merge but no -m option was given"

_CHERRY_PICK_STATUS_SUCCESS = "SUCCESS"
_CHERRY_PICK_STATUS_EMPTY = "EMPTY"

@enum.unique
class CherryPickStatus(enum.Enum):
    INITIAL = 0
    SUCCESS = 1
    ALREADY_INCLUDED = 2
    ERROR = 3
    EMPTY = 4

@dataclass
class GitCommit:
    ct: str # commiter date UNIX timestamp
    at: str # author date UNIX timestamp
    subject: str # commit subject string
    hash: str # commit hash
    cherry_pick_status: CherryPickStatus = CherryPickStatus.INITIAL

    def __str__(self)->str:
        at = datetime.fromtimestamp(float(self.at), tz=ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S%Z")
        ct = datetime.fromtimestamp(float(self.ct), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return f"{at}|{ct}|{self.hash}|{self.subject}"

class Repo:
    def __init__(self, dir: str):
        if not path.isdir(dir):
            raise FileExistsError(f"Directory {dir} does not exist")
        self.dir = dir

    def change_to_branch(self, branch: str)->str:
        """
            branch: branch name
            raise exception at error
            return output of command on success
        """
        cmd: str = f"git checkout {branch} && git pull --rebase"
        try:
            process = subprocess.run(cmd, capture_output=True,
                                 cwd=self.dir, shell=True, check=True)
            return process.stdout.decode()
        except subprocess.CalledProcessError as e:
            raise Exception(f"unable to change to branch {branch} with error: {e.stderr.decode()}") from e

    def fetch_remote(self, remote:str, branch: str)->str:
        """
        return git fetch command output str on success
        raise exception on error
        """
        cmd: str = f"git fetch {remote} {branch} 2>&1"
        try:
            process = subprocess.run(cmd, capture_output=True,
                                 cwd=self.dir, shell=True, check=True)
            return process.stdout.decode()
        except subprocess.CalledProcessError as e:
            raise Exception(f"unable to fetch {remote}/{branch} with error: {e.stdout.decode()}") from e


    def _parse_lines_to_commits(self, lines: list[str])->list[GitCommit]:
        """
        parse stdout lines from command `git --no-pager log --pretty=format:"%H|%ct|%at|%s" --since=xx --until=xx`
        return a list of GitCommit objects
        The GitCommit objects in the list is sorted by committer date
        """
        res = []
        for line in lines:
            fields = line.split("|", 3)
            if len(fields) != 4:
                logger.error(f"unable to parse line to commit: {line}")
                continue
            commit_hash, committer_date, author_date, commit_subject = fields
            res.append(GitCommit(committer_date, author_date, commit_subject, commit_hash))
        res.sort(key=lambda x: x.ct)
        return res

    def get_commits_by_range(self, since:str, until: str,
                             local_branch_name: str, remote_branch_name: str)->list[GitCommit]:
        """
        input:
            since: string of date, e.g. 2024-12-03
            until: string of date, same format as since
            branch_name: string, the name of the branch to get commits by
            return a list of GitCommit sorted by committer date in ascending order
        """
        process = subprocess.run(
            # put commit subject last as it may contain char '|'
            f'git --no-pager log --pretty=format:"%H|%ct|%at|%s" --cherry-pick --right-only --first-parent '
            f'--since="{since}" --until="{until}" {local_branch_name}...{remote_branch_name}',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.dir,
            shell=True,
        )
        if process.returncode != 0:
            raise Exception(f"unable to get commits by range: {process.stderr.decode()}")
        return self._parse_lines_to_commits([str(line, 'utf-8') for line in process.stdout.splitlines()])

    def get_commits_since_commit_hash_until_date(self, since_commit_hash:str, 
                                                 local_branch_name:str,
                                                 remote_branch_name:str,
                                                 until: str)->list[GitCommit]:
        """
        input:
            since_commit_hash: string the commit hash you want to start from, the commit hash is non-included
            until: string of date, same format as since
            branch_name: string, the name of the branch to get commits by
            return a list of GitCommit sorted by committer date in ascending order
        """
        # put commit subject last as it may contain char '|'
        cmd = f'git --no-pager log --pretty=format:"%H|%ct|%at|%s" --cherry-pick --right-only --first-parent ' \
              f'{local_branch_name}...{remote_branch_name} {since_commit_hash}..{remote_branch_name} ' \
              f'--until="{until}"'
        process = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.dir,
            shell=True,
        )
        if process.returncode != 0:
            raise Exception(f"unable to get commits with command {cmd}: {process.stderr.decode()}")
        return self._parse_lines_to_commits([str(line, 'utf-8') for line in process.stdout.splitlines()])

    def find_commit_by_subject(self, subject: str)->list[GitCommit]:
        subject = subject.replace('"', '\\"').replace("`", "\\`")
        process = subprocess.run(
            f'git --no-pager log --pretty=format:"%H|%ct|%at|%s" --grep="{subject}" -F',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.dir,
            check=True,
            shell=True,
        )
        return self._parse_lines_to_commits([str(line, 'utf-8') for line in process.stdout.splitlines()])

    def _cherry_pick(self, commit_sha: str)->tuple[bool, str]:
        """
        input: commit sha
        output:
            bool: indicate cherry pick success or not
            str: indicate status
        """
        process = subprocess.run(f"git cherry-pick {commit_sha} -m 1", stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, cwd=self.dir,
                                 shell=True)
        if process.returncode != 0:
            std_err_msg = process.stderr.decode()
            if _CHRRY_PICK_EMPTY in std_err_msg:
                return (True, _CHERRY_PICK_STATUS_EMPTY)
            if _CHERRY_PICK_MERGE in std_err_msg:
                return (True, _CHERRY_PICK_STATUS_EMPTY)
            return (False, std_err_msg)
        # remove existing Change-Id (any format)
        last_commit_msg = subprocess.check_output("git log -1 --pretty=%B", shell=True, cwd=self.dir).decode()
        last_commit_msg = re.sub(r"\n?Change-Id:[^\n]+", "", last_commit_msg)
        last_commit_msg = last_commit_msg.rstrip() + "\n"
        last_commit_msg = last_commit_msg.replace("'", "'\\''")
        process = subprocess.run(f"git commit --amend -m '{last_commit_msg}'",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           shell=True, cwd=self.dir)
        if process.returncode != 0:
            return (False, f"unable to generate changeId: {process.stderr.decode()}")
        # verify Change-Id is at the end of commit message and fix if not
        last_commit_msg = subprocess.check_output(
            "git log -1 --pretty=%B", shell=True, cwd=self.dir
        ).decode()
        change_id_match = re.search(r'Change-Id:\s*\S+', last_commit_msg)
        if change_id_match:
            change_id_line = change_id_match.group(0)
            # footer = last paragraph of the commit message, separated from the
            # body by a blank line (per git/Gerrit trailer convention)
            paragraphs = [p for p in last_commit_msg.rstrip('\n').split('\n\n') if p.strip()]
            in_footer = bool(paragraphs) and change_id_line in paragraphs[-1]
            if not in_footer:
                fixed_msg = re.sub(r'\n?' + re.escape(change_id_line) + r'\n?', '\n', last_commit_msg)
                fixed_msg = fixed_msg.rstrip() + '\n\n' + change_id_line + '\n'
                fixed_msg = fixed_msg.replace("'", "'\\''")
                process = subprocess.run(
                    f"git commit --amend -m '{fixed_msg}'",
                    shell=True, cwd=self.dir,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                if process.returncode != 0:
                    return (False, f"unable to fix Change-Id position: {process.stderr.decode()}")
        return (True, _CHERRY_PICK_STATUS_SUCCESS)

    def cherry_pick_commits(self, commits: list[GitCommit])->tuple[bool, bool, list[GitCommit]]:
        """
        Try to cherry pick commits one by one sequentially and stop at first error
        if there's any conflict, stop and keep only non empty cherry picks

        IMPORTANT: The current branch keeps all commits successfully cherry picked

        @Returns tuple (has_conflict:bool, ready_for_review:bool, commits:list[GitCommit]):
            has_conflict: flag indicates whether there's any error encountered
            ready_for_review: flag indicates if there're non empty cherry pick commits ready for review
            list of GitCommit objects: a list of GitCommit objects the function tried to cherry pick
                if has_conflict is True, results[-1] is GitCommit object that cause the error

        Raise exception if number of commits exceeds 999
        """
        if len(commits) > 999:
            raise Exception(f"Too many commits to be cherry picked: {len(commits)}")
        ready_for_review = False
        for idx, commit in enumerate(commits):
            # check if commit subject together with author date already exists in target repo
            target_repo_commits = self.find_commit_by_subject(commit.subject)
            for target_repo_commit in target_repo_commits:
                if target_repo_commit.at == commit.at and target_repo_commit.subject == commit.subject:
                    commit.cherry_pick_status = CherryPickStatus.ALREADY_INCLUDED
                    logger.debug(f"[{idx:03d}]success: {commit}")
                    logger.debug(f"    status: commit already included")
                    break
            else:
                is_success, msg = self._cherry_pick(commit.hash)
                if is_success:
                    if msg == _CHERRY_PICK_STATUS_SUCCESS:
                        ready_for_review = True
                        commit.cherry_pick_status = CherryPickStatus.SUCCESS
                    else: # empty commit
                        commit.cherry_pick_status = CherryPickStatus.EMPTY
                    logger.debug(f"[{idx:03d}]success: {commit}")
                    logger.debug(f"    status: {msg}")
                else:
                    commit.cherry_pick_status = CherryPickStatus.ERROR
                    logger.debug(f"[{idx:03d}]error  : {commit}")
                    logger.debug(f"{msg}")
                    # abort current cherry pick
                    logger.info(f"Abort cherry pick: {commit}")
                    self._abort_cherry_pick()
                    return (True, ready_for_review, commits[:idx+1])
        return (False, ready_for_review, commits)

    def _abort_cherry_pick(self):
        """
        Abort current cherry pick
        """
        cmd = f"git cherry-pick --abort 2>&1"
        try:
            subprocess.run(cmd, capture_output=True,
                                 cwd=self.dir, shell=True, check=True)
        except subprocess.CalledProcessError as e:
            raise Exception(f"Error on aborting cherry pick: {e.stdout.decode()}") from e

    def reset_hard(self, branch: str)->str:
        """
        reset hard to branch
        return output on success
        raise error on failure
        """
        cmd = f"git reset --hard {branch} 2>&1"
        try:
            process = subprocess.run(cmd, capture_output=True,
                                 cwd=self.dir, shell=True, check=True)
            return process.stdout.decode()
        except subprocess.CalledProcessError as e:
            raise Exception(f"unable to reset to {branch} with error: {e.stdout.decode()}") from e
    def git_review(self)->str:
        """
        return output on success
        """
        cmd = f"git review -Ry 2>&1"
        try:
            process = subprocess.run(cmd, capture_output=True,
                                 cwd=self.dir, shell=True, check=True)
            return process.stdout.decode()
        except subprocess.CalledProcessError as e:
            raise Exception(f"git review error: {e.stdout.decode()}") from e

    def set_remote_url(self, remote: str, url: str)->str:
        """
        set remote url
        return output on success
        raise error on failure
        """
        cmd = f"git remote set-url {remote} {url} 2>&1"
        try:
            process = subprocess.run(cmd, capture_output=True,
                                 cwd=self.dir, shell=True, check=True)
            return process.stdout.decode()
        except subprocess.CalledProcessError as e:
            raise Exception(f"unable to set remote url: {e.stdout.decode()}") from e
