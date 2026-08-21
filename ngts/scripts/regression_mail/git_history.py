"""Git and GitHub release metadata without switching the caller's checkout."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote
from urllib.request import Request, urlopen

from ngts.scripts.regression_mail.models import GitResolution, PullRequest, RcStatus
from ngts.scripts.regression_mail.normalization import (
    normalize_text,
    normalize_version,
    ordered_unique,
)


_TRAIN = re.compile(r"^(?:SONiC\.)?(\d{6})_RC\.")
_PUBLIC_REPO = "https://github.com/sonic-net/sonic-mgmt.git"
_EXACT_MGMT_PR = re.compile(
    r"^https://github\.com/sonic-net/sonic-mgmt/pull/(\d+)$"
)
_EXACT_IMAGE_PR = re.compile(
    r"^https://github\.com/sonic-net/sonic-buildimage/pull/(\d+)$"
)
_EXACT_PUBLIC_WORK_ITEM = re.compile(
    r"^https://github\.com/(sonic-net|nvidia-sonic)/"
    r"([A-Za-z0-9_.-]+)/(issues|pull)/(\d+)$",
    re.IGNORECASE,
)
_IMAGE_PR = re.compile(
    r"https://github\.com/sonic-net/sonic-buildimage/pull/\d+(?![A-Za-z0-9_-])"
)
_HASH = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
_TAG_FROM_VERSION = re.compile(r"^(\d{6}_RC\.\d+)")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHistoryResolver:
    """Resolve an exact stable-patch match and prepare an isolated source worktree."""

    def __init__(
        self,
        repo_root: Path,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        public_repo: str = _PUBLIC_REPO,
        history_limit: int = 500,
    ):
        self.repo_root = repo_root
        self.runner = runner
        self.public_repo = public_repo
        self.history_limit = history_limit

    def resolve(self, version: str) -> GitResolution:
        train = _release_train(version)
        self._git("rev-parse", "--show-toplevel")
        internal_branch = "develop-{}".format(train)
        run_key = "{}-{}".format(os.getpid(), train)
        internal_ref = "refs/regression-mail/{}/internal".format(run_key)
        temporary_refs = [internal_ref]
        remote_ref = "refs/remotes/origin/{}".format(internal_branch)
        try:
            remote_hash = self._git(
                "rev-parse",
                "--verify",
                remote_ref,
            ).stdout.strip()
            self._git("update-ref", internal_ref, remote_hash)
        except subprocess.CalledProcessError:
            self._git(
                "fetch",
                "--no-tags",
                "origin",
                "+refs/heads/{0}:{1}".format(internal_branch, internal_ref),
                timeout=300,
            )
        try:
            public_branch = self._public_branch(train)
            internal_patch_ids = self._patch_ids(internal_ref)
            internal_commits = self._lines(
                self._git(
                    "rev-list",
                    "--max-count={}".format(self.history_limit),
                    "--no-merges",
                    internal_ref,
                ).stdout
            )
            public_ref = "refs/regression-mail/{}/public/{}".format(
                run_key,
                public_branch.replace("/", "_"),
            )
            temporary_refs.append(public_ref)
            self._git(
                "fetch",
                "--no-tags",
                self.public_repo,
                "+refs/heads/{0}:{1}".format(public_branch, public_ref),
                timeout=300,
            )
            public_by_patch = self._patch_ids(public_ref)
        except Exception:
            self._delete_refs(temporary_refs)
            raise

        matched: Optional[Tuple[str, str]] = None
        for commit in internal_commits:
            patch_id = internal_patch_ids.get(commit)
            if patch_id and patch_id in public_by_patch:
                matched = (public_by_patch[patch_id], commit)
                break

        if not matched:
            self._delete_refs(temporary_refs)
            raise RuntimeError(
                "no exact stable patch-ID match found between {} and public sonic-mgmt history".format(
                    internal_branch
                )
            )
        public_hash, internal_match = matched
        try:
            branch_cutoff_date = self._branch_cutoff_date(
                internal_ref,
                train,
                internal_match,
            )
            additional = []
            for commit in internal_commits:
                if commit == internal_match:
                    break
                additional.append(commit)
        except Exception:
            self._delete_refs(temporary_refs)
            raise

        source_root = Path(
            tempfile.mkdtemp(
                prefix=".regression-mail-worktree-{}-".format(os.getpid()),
                dir=str(self.repo_root),
            )
        )
        shutil.rmtree(source_root)
        try:
            self._git("worktree", "add", "--detach", str(source_root), internal_ref, timeout=300)
        except Exception:
            shutil.rmtree(source_root, ignore_errors=True)
            self._delete_refs(temporary_refs)
            raise
        return GitResolution(
            internal_branch=internal_branch,
            public_hash=public_hash,
            source_root=source_root,
            additional_commit_hashes=additional,
            branch_cutoff_date=branch_cutoff_date,
            temporary_refs=temporary_refs,
        )

    def cleanup(self, resolution: Optional[GitResolution]) -> None:
        if not resolution or not resolution.source_root:
            return
        try:
            self._git("worktree", "remove", "--force", str(resolution.source_root), timeout=120)
        except Exception:
            shutil.rmtree(resolution.source_root, ignore_errors=True)
        self._delete_refs(resolution.temporary_refs)

    def _public_branch(self, train: str) -> str:
        completed = self.runner(
            ["git", "ls-remote", "--heads", self.public_repo],
            cwd=str(self.repo_root),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        for line in completed.stdout.splitlines():
            ref = line.split("\t")[-1]
            if ref == "refs/heads/{}".format(train):
                return train
        raise RuntimeError(
            "exact public sonic-mgmt branch {!r} does not exist".format(train)
        )

    def _branch_cutoff_date(
        self,
        internal_ref: str,
        train: str,
        fallback_commit: str,
    ) -> str:
        dates = ["{}-{}-01".format(train[:4], train[4:])]
        develop_ref = "refs/remotes/origin/develop"
        try:
            self._git("rev-parse", "--verify", develop_ref)
            merge_base = self._git(
                "merge-base",
                internal_ref,
                develop_ref,
            ).stdout.strip()
            if merge_base:
                dates.append(
                    self._git(
                        "show",
                        "-s",
                        "--format=%cs",
                        merge_base,
                    ).stdout.strip()
                )
        except subprocess.CalledProcessError:
            dates.append(
                self._git(
                    "show",
                    "-s",
                    "--format=%cs",
                    fallback_commit,
                ).stdout.strip()
            )
        return min(value for value in dates if value)

    def _patch_ids(self, ref: str) -> Dict[str, str]:
        log = subprocess.Popen(
            [
                "git",
                "-c",
                "safe.directory={}".format(self.repo_root),
                "log",
                "--no-merges",
                "--format=%H",
                "-p",
                "--max-count={}".format(self.history_limit),
                ref,
            ],
            cwd=str(self.repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        patch = subprocess.Popen(
            ["git", "patch-id", "--stable"],
            cwd=str(self.repo_root),
            stdin=log.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if log.stdout is not None:
            log.stdout.close()
        patch_stdout, patch_stderr = patch.communicate(timeout=300)
        _, log_stderr = log.communicate(timeout=300)
        if log.returncode or patch.returncode:
            detail = (log_stderr or b"").decode("utf-8", errors="replace")
            detail += patch_stderr or ""
            raise RuntimeError("git patch-id failed: {}".format(detail.strip()))
        by_commit: Dict[str, str] = {}
        by_patch: Dict[str, str] = {}
        for line in patch_stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2:
                patch_id, commit = fields[0], fields[1]
                by_commit[commit] = patch_id
                by_patch[patch_id] = commit
        if ref.endswith("/internal"):
            return by_commit
        return by_patch

    def _git(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return self.runner(
            [
                "git",
                "-c",
                "safe.directory={}".format(self.repo_root),
                *args,
            ],
            cwd=str(self.repo_root),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )

    def _delete_refs(self, refs: Sequence[str]) -> None:
        for ref in refs:
            try:
                self._git("update-ref", "-d", ref, timeout=30)
            except Exception:
                pass

    @staticmethod
    def _lines(value: str) -> List[str]:
        return [line.strip() for line in value.splitlines() if line.strip()]


def _release_train(version: str) -> str:
    match = _TRAIN.match(normalize_version(version))
    if not match:
        raise ValueError("cannot derive sonic-mgmt release train from {!r}".format(version))
    return match.group(1)


class GitHubClient:
    """Read exact SONiC release and PR metadata without GitHub CLI or AI."""

    def __init__(
        self,
        token: str = "",
        timeout: int = 60,
        opener: Callable[..., Any] = urlopen,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        rc_status_repository: str = "nvidia-sonic/sonic-buildimage",
        rc_status_ref: str = "",
        rc_status_tag: str = "",
    ):
        self.token = token.strip()
        self.timeout = timeout
        self.opener = opener
        self.runner = runner
        self.rc_status_repository = rc_status_repository.strip()
        self.rc_status_ref = rc_status_ref.strip()
        self.rc_status_tag = rc_status_tag.strip()
        if not _REPOSITORY.fullmatch(self.rc_status_repository):
            raise ValueError(
                "invalid RC_STATUS GitHub repository: {!r}".format(
                    self.rc_status_repository
                )
            )

    def fetch_rc_status(self, version: str) -> RcStatus:
        normalized_version = normalize_version(version)
        requested_match = _TAG_FROM_VERSION.match(normalized_version)
        if not requested_match:
            raise ValueError(
                "cannot derive RC tag from requested version {!r}".format(
                    version
                )
            )
        requested_tag = requested_match.group(1)
        if self.rc_status_tag:
            configured_match = _TAG_FROM_VERSION.match(
                normalize_version(self.rc_status_tag)
            )
            configured_tag = (
                configured_match.group(1)
                if configured_match
                else ""
            )
            if configured_tag != requested_tag:
                raise ValueError(
                    "configured RC_STATUS tag {!r} does not match "
                    "requested release {!r}".format(
                        self.rc_status_tag,
                        requested_tag,
                    )
                )
        ref = self.rc_status_ref or requested_tag
        payload = self._get_json(
            "/repos/{}/contents/RC_STATUS.md?ref={}".format(
                self.rc_status_repository,
                quote(ref, safe=""),
            )
        )
        if not isinstance(payload, Mapping):
            raise ValueError("GitHub returned a non-object RC_STATUS response")
        try:
            content = str(payload["content"]).replace("\n", "")
            markdown = base64.b64decode(content).decode("utf-8")
        except (KeyError, ValueError, UnicodeDecodeError) as error:
            raise ValueError(
                "GitHub returned invalid RC_STATUS.md content"
            ) from error
        return _parse_rc_status(
            markdown,
            requested_tag,
        )

    def get_mgmt_prs(self, urls: Iterable[str]) -> List[PullRequest]:
        return self._get_pull_requests(
            urls,
            _EXACT_MGMT_PR,
            "sonic-net/sonic-mgmt",
        )

    def get_open_mgmt_prs_by_authors(
        self,
        authors: Iterable[str],
    ) -> List[PullRequest]:
        """List public open sonic-mgmt PRs without relying on a remote CSV."""

        author_keys = {
            normalize_text(author).casefold()
            for author in authors
            if normalize_text(author)
        }
        if not author_keys:
            raise ValueError("at least one GitHub author is required")

        result: List[PullRequest] = []
        page = 1
        while page <= 20:
            payload = self._get_json(
                "/repos/sonic-net/sonic-mgmt/pulls"
                "?state=open&per_page=100&page={}".format(page)
            )
            if not isinstance(payload, list):
                raise ValueError("GitHub pull request list response is not an array")
            for item in payload:
                if not isinstance(item, Mapping):
                    raise ValueError("GitHub pull request list contains a non-object")
                user = item.get("user") or {}
                author = normalize_text(user.get("login"))
                if author.casefold() not in author_keys:
                    continue
                url = normalize_text(item.get("html_url"))
                if not _EXACT_MGMT_PR.fullmatch(url):
                    raise ValueError(
                        "GitHub returned an unexpected sonic-mgmt PR URL: {!r}".format(
                            url
                        )
                    )
                result.append(
                    PullRequest(
                        url=url,
                        title=normalize_text(item.get("title")),
                        author=author,
                        state=normalize_text(item.get("state")),
                        draft=item.get("draft") is True,
                        labels=_pull_request_labels(item),
                    )
                )
            if len(payload) < 100:
                return result
            page += 1
        raise RuntimeError("GitHub open pull request pagination exceeded 20 pages")

    def get_image_prs(self, urls: Iterable[str]) -> List[PullRequest]:
        return self._get_pull_requests(
            urls,
            _EXACT_IMAGE_PR,
            "sonic-net/sonic-buildimage",
        )

    def inspect_issue_links(
        self,
        urls: Iterable[str],
    ) -> Dict[str, Mapping[str, Any]]:
        """Fetch exact public issue or PR evidence for semantic review."""

        requested = ordered_unique(urls)
        result: Dict[str, Mapping[str, Any]] = {}
        errors: List[Exception] = []
        for url in requested:
            match = _EXACT_PUBLIC_WORK_ITEM.fullmatch(url)
            if not match:
                raise ValueError(
                    "non-public or unexpected GitHub URL rejected: {!r}".format(
                        url
                    )
                )
            owner, repository, kind, number = match.groups()
            endpoint = "pulls" if kind.casefold() == "pull" else "issues"
            try:
                payload = self._get_json(
                    "/repos/{}/{}/{}/{}".format(
                        owner,
                        repository,
                        endpoint,
                        number,
                    )
                )
                if not isinstance(payload, Mapping):
                    raise ValueError(
                        "GitHub issue or pull request response is not an object"
                    )
                if normalize_text(payload.get("html_url")).casefold() != url.casefold():
                    raise ValueError(
                        "GitHub metadata URL mismatch for {!r}".format(url)
                    )
                result[url] = {
                    "url": url,
                    "kind": kind.casefold(),
                    "title": normalize_text(payload.get("title")),
                    "body": _bounded_public_work_item_body(
                        payload.get("body")
                    ),
                    "state": normalize_text(payload.get("state")),
                    "merged": bool(
                        payload.get("merged") or
                        payload.get("merged_at")
                    ),
                    "labels": [
                        normalize_text(label.get("name"))
                        for label in payload.get("labels") or []
                        if isinstance(label, Mapping) and
                        normalize_text(label.get("name"))
                    ],
                }
            except Exception as error:
                errors.append(error)
        if requested and not result and errors:
            raise errors[0]
        return result

    def _get_pull_requests(
        self,
        urls: Iterable[str],
        pattern: re.Pattern,
        repository: str,
    ) -> List[PullRequest]:
        result: List[PullRequest] = []
        for url in ordered_unique(urls):
            match = pattern.fullmatch(url)
            if not match:
                raise ValueError(
                    "non-public or unexpected PR URL rejected for {}: {!r}".format(
                        repository,
                        url,
                    )
                )
            payload = self._get_json(
                "/repos/{}/pulls/{}".format(
                    repository,
                    match.group(1),
                )
            )
            if not isinstance(payload, Mapping):
                raise ValueError("GitHub pull request response is not an object")
            html_url = str(payload.get("html_url") or "").strip()
            if html_url != url:
                raise ValueError(
                    "GitHub metadata URL mismatch for {!r}".format(url)
                )
            user = payload.get("user") or {}
            result.append(
                PullRequest(
                    url=url,
                    title=str(payload.get("title") or "").strip(),
                    author=str(user.get("login") or "").strip(),
                    state=str(payload.get("state") or "").strip(),
                    draft=payload.get("draft") is True,
                    labels=_pull_request_labels(payload),
                )
            )
        return result

    def _get_json(self, path: str) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "sonic-regression-mail/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = Request("https://api.github.com" + path, headers=headers)
        with self.opener(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def _pull_request_labels(payload: Mapping[str, Any]) -> Tuple[str, ...]:
    return tuple(
        normalize_text(label.get("name"))
        for label in payload.get("labels") or []
        if isinstance(label, Mapping) and
        normalize_text(label.get("name"))
    )


def _bounded_public_work_item_body(value: object, limit: int = 12000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return "{}\n...\n{}".format(text[:half], text[-half:])


def _parse_rc_status(markdown: str, expected_version: str) -> RcStatus:
    tag = _table_value(markdown, "Tag")
    expected_tag_match = _TAG_FROM_VERSION.match(
        normalize_version(expected_version)
    )
    if not expected_tag_match:
        raise ValueError(
            "cannot derive RC tag from requested version {!r}".format(
                expected_version
            )
        )
    expected_tag = expected_tag_match.group(1)
    if _clean_markdown_value(tag) != expected_tag:
        raise ValueError(
            "RC_STATUS.md Tag {!r} does not match requested release tag {!r}".format(
                _clean_markdown_value(tag),
                expected_tag,
            )
        )

    branch = _clean_markdown_value(_table_value(markdown, "RC branch"))
    upstream = _clean_markdown_value(_table_value(markdown, "Upstream base"))
    hash_match = _HASH.search(upstream)
    if not branch:
        raise ValueError("RC_STATUS.md does not contain RC branch")
    if not hash_match:
        raise ValueError(
            "RC_STATUS.md does not contain a valid Upstream base hash"
        )
    return RcStatus(
        image_branch=branch,
        image_public_hash=hash_match.group(0),
        image_pr_urls=ordered_unique(_IMAGE_PR.findall(markdown)),
    )


def _table_value(markdown: str, label: str) -> str:
    escaped = re.escape(label)
    patterns = (
        re.compile(
            r"^\|\s*{}\s*\|\s*(.*?)\s*\|?\s*$".format(escaped),
            re.IGNORECASE | re.MULTILINE,
        ),
        re.compile(
            r"^\s*[-*]\s*\*\*{}:\*\*\s*(.+?)\s*$".format(escaped),
            re.IGNORECASE | re.MULTILINE,
        ),
        re.compile(
            r"^\s*[-*]?\s*{}\s*:\s*(.+?)\s*$".format(escaped),
            re.IGNORECASE | re.MULTILINE,
        ),
    )
    for pattern in patterns:
        match = pattern.search(markdown)
        if match:
            return match.group(1)
    return ""


def _clean_markdown_value(value: str) -> str:
    text = normalize_text(value).strip("`")
    link = re.fullmatch(r"\[([^\]]+)\]\([^)]+\)", text)
    return normalize_text(link.group(1) if link else text)
