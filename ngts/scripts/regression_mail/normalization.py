"""Deterministic normalization helpers."""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, List, Optional


_PARAMETERS = re.compile(r"\[[^\[\]]*\]$")
_SPACE = re.compile(r"\s+")
_REDMINE_URL = re.compile(
    r"(?<![\w.-])(?:https?://)?redmine\.mellanox\.com/issues/(\d+)",
    re.IGNORECASE,
)
_REDMINE_REFERENCE = re.compile(
    r"(?i)\b(?:(?:redmine|rm)[ \t]*(?:(?:issue|bug|ticket)[ \t]*)?"
    r"|(?:issue|bug|ticket)[ \t]*)(?:id[ \t]*)?[#:]?[ \t]*\d+\b"
)
_REDMINE_REFERENCE_ID = re.compile(
    r"(?i)\b(?:redmine|rm)[ \t]*(?:(?:issue|bug|ticket)[ \t]*)?"
    r"(?:id[ \t]*)?[#:]?[ \t]*(\d+)\b"
)
_REDMINE_SUMMARY_ID = re.compile(
    r"(?im)^[ \t]*\[[^\]\r\n]{1,160}\][ \t]+"
    r"(?:Bug[ \t]+SW|Feature|Issue)[ \t]+#(\d+)[ \t]*:"
)
_FAILURE_MESSAGE_REDMINE_REFERENCE_ID = re.compile(
    r"(?i)\b(?:redmine|rm)[ \t]+(?:issue|bug|ticket)[ \t]*"
    r"(?:id[ \t]*)?[#:]?[ \t]*(\d+)\b"
)
_FAILURE_MESSAGE_BARE_REDMINE_REFERENCE_ID = re.compile(
    r"(?i)\b(?:redmine|rm)[ \t]+[#:]?[ \t]*(\d{5,})\b"
)
_URL = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_PUBLIC_SONIC_URL = re.compile(
    r"^https://github\.com/(?:sonic-net|nvidia-sonic)/[A-Za-z0-9_.-]+/"
    r"(?:issues|pull)/\d+/?$",
    re.IGNORECASE,
)
_PUBLIC_ISSUE_URL = re.compile(
    r"https://github\.com/(?:sonic-net|nvidia-sonic)/[A-Za-z0-9_.-]+/issues/\d+",
    re.IGNORECASE,
)
_PUBLIC_PR_URL = re.compile(
    r"https://github\.com/(?:sonic-net|nvidia-sonic)/[A-Za-z0-9_.-]+/pull/\d+",
    re.IGNORECASE,
)
_INTERNAL_CHANGE_URL = re.compile(
    r"https://git-[A-Za-z0-9.-]+/r/c/[^\s<>()]+/\+/(\d+)(?!\d)",
    re.IGNORECASE,
)
COMMENT_SEPARATOR = "\n\n--------------------\n\n"


def normalize_version(value: object) -> str:
    version = str(value or "").strip()
    if version.lower().startswith("sonic."):
        version = version[6:]
    return version


def normalize_text(value: object) -> str:
    return _SPACE.sub(" ", str(value or "")).strip()


def canonical_test_name(value: object) -> str:
    name = normalize_text(value)
    return _PARAMETERS.sub("", name)


def stable_record_id(version: str, session_id: str, key_id: str, test_name: str) -> str:
    identity = "\x1f".join(
        (
            normalize_version(version),
            normalize_text(session_id),
            normalize_text(key_id),
            normalize_text(test_name),
        )
    )
    return "row-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def unique_sorted(values: Iterable[str]) -> List[str]:
    return sorted({normalize_text(value) for value in values if normalize_text(value)})


def ordered_unique(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def join_comment_blocks(values: Iterable[object]) -> str:
    return COMMENT_SEPARATOR.join(
        ordered_unique(
            block.strip()
            for value in values
            for block in str(value or "").split(COMMENT_SEPARATOR)
            if block.strip()
        )
    )


def join_distinct_analysis_blocks(values: Iterable[object]) -> str:
    """Join analysis while dropping conservative near-duplicate prose."""

    blocks = ordered_unique(
        block.strip()
        for value in values
        for block in str(value or "").split(COMMENT_SEPARATOR)
        if block.strip()
    )
    selected: List[str] = []
    for block in blocks:
        comparable = _analysis_comparable(block)
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(selected)
                if comparable and
                comparable == _analysis_comparable(existing)
            ),
            None,
        )
        if duplicate_index is None:
            selected.append(block)
        elif len(block) > len(selected[duplicate_index]):
            selected[duplicate_index] = block
    return COMMENT_SEPARATOR.join(selected)


def strip_redmine_references(value: object) -> str:
    text = _REDMINE_URL.sub("", normalize_text(value))
    text = _REDMINE_REFERENCE.sub("", text)
    return normalize_text(text.strip(" -;,"))


def sanitize_public_comment(
    value: object,
    limit: Optional[int] = 500,
) -> str:
    """Remove internal ticket references and bound deterministic email prose."""

    text = strip_redmine_references(value)
    text = _URL.sub(
        lambda match: (
            match.group(0)
            if _PUBLIC_SONIC_URL.fullmatch(match.group(0).rstrip(".,;"))
            else ""
        ),
        text,
    )
    text = normalize_text(text.strip(" -;,"))
    if limit is None or len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def extract_redmine_urls(value: object) -> List[str]:
    return [
        "https://redmine.mellanox.com/issues/{}".format(issue_id)
        for issue_id in extract_redmine_issue_ids(value)
    ]


def extract_redmine_issue_ids(value: object) -> List[str]:
    text = str(value or "")
    return unique_sorted(
        [match.group(1) for match in _REDMINE_URL.finditer(text)] +
        [match.group(1) for match in _REDMINE_REFERENCE_ID.finditer(text)] +
        [match.group(1) for match in _REDMINE_SUMMARY_ID.finditer(text)]
    )


def extract_failure_message_redmine_urls(value: object) -> List[str]:
    """Extract only explicit Redmine references from raw failure output."""

    text = str(value or "")
    issue_ids = unique_sorted(
        [match.group(1) for match in _REDMINE_URL.finditer(text)] +
        [
            match.group(1)
            for match in _FAILURE_MESSAGE_REDMINE_REFERENCE_ID.finditer(text)
        ] +
        [
            match.group(1)
            for match in _FAILURE_MESSAGE_BARE_REDMINE_REFERENCE_ID.finditer(
                text
            )
        ]
    )
    return [
        "https://redmine.mellanox.com/issues/{}".format(issue_id)
        for issue_id in issue_ids
    ]


def retain_redmine_references(
    value: object,
    allowed: Iterable[object],
) -> str:
    """Drop Redmine references whose issue IDs are outside ``allowed``."""

    allowed_ids = {
        issue_id
        for reference in allowed
        for issue_id in extract_redmine_issue_ids(reference)
    }
    text = str(value or "")
    text = _REDMINE_URL.sub(
        lambda match: (
            match.group(0)
            if match.group(1) in allowed_ids
            else ""
        ),
        text,
    )
    text = _REDMINE_REFERENCE_ID.sub(
        lambda match: (
            match.group(0)
            if match.group(1) in allowed_ids
            else ""
        ),
        text,
    )
    return normalize_text(text)


def extract_public_issue_urls(value: object) -> List[str]:
    return unique_sorted(match.group(0) for match in _PUBLIC_ISSUE_URL.finditer(str(value or "")))


def extract_public_pr_urls(value: object) -> List[str]:
    return unique_sorted(
        match.group(0)
        for match in _PUBLIC_PR_URL.finditer(str(value or ""))
    )


def extract_internal_change_urls(value: object) -> List[str]:
    return unique_sorted(
        match.group(0)
        for match in _INTERNAL_CHANGE_URL.finditer(
            str(value or "")
        )
    )


def strip_public_issue_urls(value: object) -> str:
    """Remove public issue URLs and dangling boolean syntax from a condition."""

    text = _PUBLIC_ISSUE_URL.sub("", str(value or ""))
    previous = None
    while text != previous:
        previous = text
        text = re.sub(
            r"(?i)\b(?:and|or)\b(?=\s*(?:\)|$|[,;]))",
            "",
            text,
        )
        text = re.sub(
            r"(?i)(^|[(,;])\s*\b(?:and|or)\b",
            r"\1",
            text,
        )
        text = re.sub(
            r"(?i)\b(and|or)\s+(?:and|or)\b",
            r"\1",
            text,
        )
        text = re.sub(r"\(\s*\)", "", text)
    text = normalize_text(text.strip(" ,;:-"))
    enclosing = re.fullmatch(r"\(\s*([^()]*)\s*\)", text)
    while enclosing:
        text = normalize_text(enclosing.group(1))
        enclosing = re.fullmatch(r"\(\s*([^()]*)\s*\)", text)
    return text


def _analysis_comparable(value: object) -> str:
    text = _URL.sub("", str(value or "")).casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return normalize_text(text)
