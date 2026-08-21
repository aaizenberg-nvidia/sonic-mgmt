"""NVIDIA Inference API adapter for concise internal-comment summaries."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ngts.scripts.regression_mail.models import SemanticGroup
from ngts.scripts.regression_mail.normalization import (
    extract_redmine_issue_ids,
    normalize_text,
)
from ngts.scripts.regression_mail.redaction import SetupRedactor


_REDMINE_URL = re.compile(
    r"(?<![\w.-])(?:https?://)?redmine\.mellanox\.com/issues/\d+"
    r"(?:[/?#][^\s]*)?",
    re.IGNORECASE,
)
_REDMINE_REFERENCE = re.compile(
    r"\b(?:redmine|rm|internal\s+(?:issue|ticket)|issue|ticket)"
    r"\s*(?:id\s*)?[#:]?\s*\d+\b",
    re.IGNORECASE,
)
_REDMINE_WORD = re.compile(r"\bredmine\b", re.IGNORECASE)
_TICKET_ID = re.compile(
    r"(?:redmine\.mellanox\.com/issues/|"
    r"(?:redmine|rm)\s*(?:(?:issue|bug|ticket)\s*)?"
    r"(?:id\s*)?[#:]?\s*|"
    r"(?:internal\s+)?(?:issue|bug|ticket)\s*(?:id\s*)?[#:]?\s*)"
    r"(\d+)",
    re.IGNORECASE,
)
_HASH_TICKET_ID = re.compile(r"(?<!\w)#\s*(\d{5,9})\b")
_SPACE = re.compile(r"[ \t]+")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_MAC_ADDRESS = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}"
    r"(?![0-9a-f])"
)
_IPV6_CANDIDATE = re.compile(
    r"(?i)(?<![\w:])[0-9a-f:]*:[0-9a-f:]+(?![\w:])"
)
_UUID = re.compile(
    r"(?i)(?<![0-9a-f])"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![0-9a-f])"
)
_EMAIL = re.compile(
    r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"
    r"(?![\w.-])"
)
_DYNAMIC_NUMBER = re.compile(
    r"(?i)<Value>|"
    r"(?<![A-Za-z0-9_])[-+]?(?:0x[0-9a-f]+|\d+)"
    r"(?![A-Za-z0-9_])"
)
_INTERNAL_CHANGE_URL = re.compile(
    r"https?://git-nbu-sw\.nvidia\.com/r/c/[^\s<>()]+/\+/(\d+)(?!\d)",
    re.IGNORECASE,
)
_INTERNAL_CHANGE_ID = re.compile(r"(?i)\bChange-Id:\s*I[0-9a-f]{40}\b")
_COMMIT_HASH = re.compile(r"(?i)\b[0-9a-f]{40}\b")
_FAILURE_SIGNAL = re.compile(
    r"(?i)(?:exception|err(?:or)?|failed|failure|traceback|assert|"
    r"module_stderr|stderr|stdout|msg\s*=|rc\s*=)"
)
_ERROR_PROCESS = re.compile(
    r"^(ERR(?:OR)?\s+)([^:]+)(:.*)$",
    re.IGNORECASE,
)
_ERROR_LOG_LINE = re.compile(
    r"^ERR(?:OR)?\s+[^:\r\n]{1,120}:",
    re.IGNORECASE,
)
_DISPLAY_ERROR_PLACEHOLDER = re.compile(
    r"(?i)(?:x\.x\.x\.x|"
    r"<(?:IPv6|MAC|ID|User|ReturnedValue|PID|Port|Process|Interface|"
    r"RuntimeNumber|Line|LAG_ID|OID|Version|ErrorCode|AttributeIndex|"
    r"SessionID|BridgeID|PortID|ASIC_ID)>)"
)
_TRAILING_TICKET_PROSE = re.compile(
    r"(?i)\s+(?:Expected behavior|Actual behavior|Steps to reproduce)"
    r"(?:\s*:.*)?$"
)
_ERR_SEVERITY = re.compile(r"\bERR(?:OR)?\b", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
_MAX_FAILURE_EVIDENCE_BATCH_CHARS = 48000
_MAX_ERROR_PATTERN_BATCH_CHARS = 48000


class AiCommentSummarizer:
    """Batch semantic groups through the OpenAI Responses API."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        batch_size: int = 8,
        timeout: int = 120,
        client_factory: Optional[Callable[..., Any]] = None,
    ):
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.batch_size = batch_size
        self.timeout = timeout
        self.client_factory = client_factory

    def summarize(
        self,
        groups: Sequence[SemanticGroup],
        redactor: SetupRedactor,
    ) -> Dict[str, str]:
        if not groups:
            return {}
        if not self.api_key:
            raise RuntimeError("INFERENCE_API_KEY is not configured")
        client = self._client()
        summaries: Dict[str, str] = {}
        for offset in range(0, len(groups), self.batch_size):
            batch = list(groups[offset: offset + self.batch_size])
            parsed = self._request_batch(client, batch)
            for group in batch:
                summary = sanitize_ai_summary(parsed[group.group_id], group, redactor)
                if group.internal_comments.strip() and not summary:
                    raise ValueError(
                        "AI returned an empty summary for a non-empty group"
                    )
                summaries[group.group_id] = summary
        return summaries

    def review_redmine_links(
        self,
        contexts: Mapping[str, Mapping[str, Any]],
        redactor: SetupRedactor,
    ) -> Dict[str, str]:
        return self.review_public_links(contexts, redactor)

    def review_public_links(
        self,
        contexts: Mapping[str, Mapping[str, Any]],
        redactor: SetupRedactor,
    ) -> Dict[str, str]:
        """Select only public links that describe or fix the same issue."""

        if not contexts:
            return {}
        decisions = {
            url: "none"
            for url, context in contexts.items()
            if not (
                context.get("github_issue_urls") or
                context.get("github_pr_urls")
            )
        }
        candidates = [
            (url, context)
            for url, context in contexts.items()
            if context.get("github_issue_urls") or
            context.get("github_pr_urls")
        ]
        if not candidates:
            return decisions
        if not self.api_key:
            raise RuntimeError("INFERENCE_API_KEY is not configured")
        client = self._client()
        for offset in range(0, len(candidates), self.batch_size):
            batch = candidates[offset: offset + self.batch_size]
            parsed = self._request_redmine_link_batch(
                client,
                batch,
                redactor,
            )
            decisions.update(parsed)
        return decisions

    def classify_failure_causes(
        self,
        groups: Sequence[SemanticGroup],
        file_keys: Mapping[str, str],
        redactor: SetupRedactor,
    ) -> Dict[str, str]:
        """Assign one canonical semantic cause key across Python files."""

        if not groups:
            return {}
        if not self.api_key:
            raise RuntimeError("INFERENCE_API_KEY is not configured")
        result: Dict[str, str] = {}
        unscoped: List[SemanticGroup] = []
        for group in groups:
            file_key = file_keys.get(group.group_id, "")
            if not file_key:
                raise ValueError(
                    "failure cause classification requires a Python file"
                )
            issue_refs = _failure_issue_refs(group)
            if issue_refs:
                digest = hashlib.sha256(
                    "\x1f".join(issue_refs).encode("utf-8")
                ).hexdigest()
                result[group.group_id] = "issue:" + digest
                continue
            unscoped.append(group)

        if not unscoped:
            return result
        client = self._client()
        evidence_by_group: Dict[str, List[str]] = {
            group.group_id: []
            for group in unscoped
        }
        records = [
            (
                "{}:{}".format(group.group_id, index),
                group.group_id,
                chunk,
            )
            for group in unscoped
            for index, chunk in enumerate(
                _failure_evidence_chunks(
                    group.cause_evidence or group.comments
                )
            )
        ]
        for batch in _failure_evidence_batches(records):
            extracted = self._request_failure_evidence_batch(
                client,
                batch,
            )
            for evidence_id, group_id, _chunk in batch:
                value = normalize_text(extracted[evidence_id])
                if value and value.casefold() != "insufficient evidence":
                    evidence_by_group[group_id].append(value)
        qualified = [
            group
            for group in unscoped
            if evidence_by_group[group.group_id]
        ]
        for group in unscoped:
            if group not in qualified:
                result[group.group_id] = "unclassified:" + group.group_id
        if not qualified:
            return result
        parsed = self._request_failure_cause_batch(
            client,
            qualified,
            evidence_by_group,
            file_keys,
        )
        for group in qualified:
            value = normalize_text(parsed[group.group_id])
            if not value or len(value) > 500:
                raise ValueError(
                    "AI failure cause keys must contain 1-500 characters"
                )
            redactor.assert_clean([value], "AI failure cause key")
            result[group.group_id] = value
        return result

    def extract_error_logs(
        self,
        groups: Sequence[SemanticGroup],
        redactor: SetupRedactor,
    ) -> Dict[str, str]:
        """Use AI to deduplicate ERR sentence patterns without rewriting them."""

        if not groups:
            return {}
        extracted = {
            group.group_id: sanitize_error_logs(
                group.comments,
                group,
                redactor,
            )
            for group in groups
        }
        records = [
            (
                "{}:{}".format(group.group_id, index),
                group.group_id,
                line,
            )
            for group in groups
            for index, line in enumerate(
                extracted.get(group.group_id, "").splitlines()
            )
            if line.strip()
        ]
        if not records:
            return extracted
        representative_records, structural_representatives = (
            _structural_error_representatives(records)
        )
        batches = _error_pattern_batches(representative_records)
        if len(batches) != 1:
            raise RuntimeError(
                "error pattern evidence exceeds one global AI request"
            )
        client = self._client()
        ai_representatives = self._request_error_pattern_batch(
            client,
            batches[0],
        )
        representative_ids = {
            line_id: ai_representatives[
                structural_representatives[line_id]
            ]
            for line_id, _group_id, _line in records
        }
        return self._consolidate_error_logs(
            groups,
            extracted,
            representative_ids,
        )

    def _client(self) -> Any:
        factory = self.client_factory
        if factory is None:
            try:
                from openai import OpenAI
            except ImportError as error:
                raise RuntimeError(
                    "openai SDK is required for AI comment summaries"
                ) from error
            factory = OpenAI
        return factory(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
        )

    def _request_batch(
        self,
        client: Any,
        groups: Sequence[SemanticGroup],
    ) -> Mapping[str, str]:
        requested_ids = [group.group_id for group in groups]
        payload = [
            {
                "group_id": group.group_id,
                "internal_comments": group.internal_comments,
            }
            for group in groups
        ]
        response = client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Summarize each SONiC regression internal comment concisely "
                        "and generally. Remove setup names, Redmine URLs, ticket "
                        "numbers, owner names, and other internal identifiers. "
                        "Return only one JSON object mapping every supplied group_id "
                        "to its summary. Preserve empty input as an empty summary."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            max_output_tokens=1024,
        )
        return _strict_json_mapping(response, requested_ids, "summaries")

    def _request_redmine_link_batch(
        self,
        client: Any,
        records: Sequence[Tuple[str, Mapping[str, Any]]],
        redactor: SetupRedactor,
    ) -> Mapping[str, str]:
        ids_by_url = {
            url: "ticket-{}".format(index)
            for index, (url, _context) in enumerate(records)
        }
        url_by_id = {
            record_id: url
            for url, record_id in ids_by_url.items()
        }
        payload = []
        allowed_by_id: Dict[str, Set[str]] = {}
        for url, context in records:
            record_id = ids_by_url[url]
            issue_urls = list(context.get("github_issue_urls") or ())
            pr_urls = list(context.get("github_pr_urls") or ())
            link_details = context.get("github_link_details") or {}
            evidence = _sanitize_redmine_link_evidence(
                context.get("evidence"),
                redactor,
            )
            payload.append(
                {
                    "ticket_id": record_id,
                    "subject": _sanitize_redmine_link_evidence(
                        context.get("subject"),
                        redactor,
                    ),
                    "current_failure_evidence": (
                        _sanitize_redmine_link_evidence(
                            context.get("current_failure_evidence"),
                            redactor,
                        )
                    ),
                    "internal_ticket_evidence": evidence,
                    "github_issue_candidates": [
                        _public_link_evidence(
                            value,
                            link_details.get(value),
                            redactor,
                        )
                        for value in issue_urls
                    ],
                    "github_pr_candidates": [
                        _public_link_evidence(
                            value,
                            link_details.get(value),
                            redactor,
                        )
                        for value in pr_urls
                    ],
                }
            )
            allowed_by_id[record_id] = (
                {"none"} |
                {"github_issue:" + value for value in issue_urls} |
                {"github_pr:" + value for value in pr_urls}
            )
        response = client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Review public GitHub links mentioned in each internal "
                        "SONiC ticket. Return only one JSON object mapping every "
                        "ticket_id to exactly one allowed decision. Select "
                        "'github_pr:<candidate>' only when the evidence clearly "
                        "shows that candidate PR fixes the same underlying "
                        "failure as current_failure_evidence. "
                        "Otherwise select 'github_issue:<candidate>' only when "
                        "the candidate issue content, internal ticket evidence, "
                        "and current failure evidence describe the same defect "
                        "or a concrete causal chain. A lower-level issue is "
                        "related when its failure directly explains the current "
                        "symptom or missing output, even when the components or "
                        "exception strings differ. Do not select a mere external "
                        "dependency, example, or unrelated reference without "
                        "that causal connection. Similar wording alone is not "
                        "enough. Prefer a confirmed fixing PR over an issue. "
                        "Return 'none' when candidate metadata is missing or the "
                        "relationship is ambiguous. Copy candidate URLs "
                        "byte-for-byte and never invent a URL."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            max_output_tokens=2048,
        )
        parsed = _strict_json_mapping(
            response,
            list(url_by_id),
            "Redmine link decisions",
        )
        result: Dict[str, str] = {}
        for record_id, decision in parsed.items():
            if decision not in allowed_by_id[record_id]:
                raise ValueError(
                    "AI returned an invalid Redmine link decision"
                )
            result[url_by_id[record_id]] = decision
        return result

    def _request_failure_evidence_batch(
        self,
        client: Any,
        records: Sequence[Tuple[str, str, str]],
    ) -> Mapping[str, str]:
        requested_ids = [
            evidence_id
            for evidence_id, _group_id, _chunk in records
        ]
        payload = [
            {
                "evidence_id": evidence_id,
                "evidence": chunk,
            }
            for evidence_id, _group_id, chunk in records
        ]
        instruction = (
            "Extract the underlying SONiC failure cause from each "
            "preprocessed evidence chunk. Return only one JSON object "
            "mapping every evidence_id to a concise structured cause "
            "summary containing the exception/error class, failing "
            "operation, and terminal reason when present. Every value "
            "must be one JSON string, never an array, object, number, "
            "boolean, or null. Ignore PTF command arguments, setup/device "
            "names, addresses, timestamps, parameters, line numbers, and "
            "runtime IDs. Do not infer missing facts. Return exactly "
            "'insufficient evidence' when the chunk has no usable "
            "root-cause signal. Generic wrappers such as an Ansible module "
            "failure, a non-zero command return code, or a statement that a "
            "test failed are insufficient unless the terminal operation and "
            "reason are also present."
        )
        last_error: Optional[ValueError] = None
        for attempt in range(2):
            response = client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": instruction + (
                            " The previous response violated the JSON schema; "
                            "correct it and ensure every value is a JSON string."
                            if attempt
                            else ""
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                max_output_tokens=4096,
            )
            try:
                return _strict_json_mapping(
                    response,
                    requested_ids,
                    "failure evidence",
                )
            except ValueError as error:
                last_error = error
        raise last_error

    def _request_failure_cause_batch(
        self,
        client: Any,
        groups: Sequence[SemanticGroup],
        evidence_by_group: Mapping[str, Sequence[str]],
        file_keys: Mapping[str, str],
    ) -> Mapping[str, str]:
        requested_ids = [group.group_id for group in groups]
        payload = [
            {
                "group_id": group.group_id,
                "python_file": file_keys.get(group.group_id, ""),
                "root_cause_evidence": list(
                    evidence_by_group.get(group.group_id, ())
                ),
                "dashboard_analysis": group.internal_comments,
                "dashboard_status": group.internal_review_status,
            }
            for group in groups
        ]
        instruction = (
            "Classify SONiC failure groups across Python files by underlying "
            "error cause. Return only one JSON "
            "object mapping every group_id to a canonical cause key. "
            "Every value must be one JSON string, never an array, object, "
            "number, boolean, or null. Use the same byte-identical key only "
            "when exception/error class, failing operation, and terminal "
            "reason indicate the same root cause. Ignore timestamps, "
            "setup/device names, IP/MAC addresses, parameters, line numbers, "
            "IDs, changing runtime values, and Python file boundaries. Similar "
            "wording alone is not "
            "enough; different root causes must have different keys. Use "
            "root_cause_evidence as primary evidence and dashboard_analysis "
            "as supporting evidence. Never group items merely because both "
            "are not analyzed/not found. When evidence is insufficient or "
            "ambiguous, return the unique key 'unclassified:<group_id>'. Do "
            "not include URLs, ticket numbers, setup names, or prose outside "
            "JSON."
        )
        last_error: Optional[ValueError] = None
        for attempt in range(2):
            response = client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": instruction + (
                            " The previous response violated the JSON schema; "
                            "correct it and ensure every value is a JSON string."
                            if attempt
                            else ""
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                max_output_tokens=8192,
            )
            try:
                return _strict_json_mapping(
                    response,
                    requested_ids,
                    "failure causes",
                )
            except ValueError as error:
                last_error = error
        raise last_error

    def _request_error_pattern_batch(
        self,
        client: Any,
        records: Sequence[Tuple[str, str, str]],
    ) -> Mapping[str, str]:
        requested_ids = [
            line_id
            for line_id, _group_id, _line in records
        ]
        payload = [
            {
                "line_id": line_id,
                "group_id": group_id,
                "error_log": line,
                "structural_hint": error_log_signature(line),
            }
            for line_id, group_id, line in records
        ]
        instruction = (
            "Classify SONiC ERR/ERROR lines by sentence pattern and "
            "underlying failure meaning. Return only one JSON object mapping "
            "every line_id to the earliest equivalent line_id from this "
            "request. A representative must have the same group_id as the "
            "source line, and a representative line must map to itself. "
            "Use the same representative when lines have "
            "the same operation and terminal reason and differ only "
            "in timestamp, device/process instance, IP, port, PID, "
            "interface, returned value, object ID, line number, or "
            "another runtime value. Keep different operations or "
            "terminal reasons separate. structural_hint is internal "
            "comparison evidence only. Do not return source log text, a "
            "summary, or an invented key. Do not omit any line_id. Every JSON "
            "value must be one supplied line_id string."
        )
        last_error: Optional[ValueError] = None
        for attempt in range(2):
            response = client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": instruction + (
                            " The previous response violated the JSON schema; "
                            "return every requested line_id with a string value."
                            if attempt
                            else ""
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                max_output_tokens=8192,
            )
            try:
                parsed = _strict_json_mapping(
                    response,
                    requested_ids,
                    "error representatives",
                )
                _validate_error_representatives(
                    parsed,
                    requested_ids,
                    {
                        line_id: group_id
                        for line_id, group_id, _line in records
                    },
                )
                return parsed
            except ValueError as error:
                last_error = error
        raise last_error

    def _consolidate_error_logs(
        self,
        groups: Sequence[SemanticGroup],
        extracted: Mapping[str, str],
        representative_ids: Mapping[str, str],
    ) -> Dict[str, str]:
        records = [
            (
                "{}:{}".format(group.group_id, index),
                group.group_id,
                line,
            )
            for group in groups
            for index, line in enumerate(
                extracted.get(group.group_id, "").splitlines()
            )
            if line.strip()
        ]
        result: Dict[str, List[str]] = {
            group.group_id: []
            for group in groups
        }
        source_by_id = {
            line_id: (group_id, line)
            for line_id, group_id, line in records
        }
        for line_id, group_id, line in records:
            representative_id = representative_ids.get(line_id, line_id)
            representative_group, representative = source_by_id.get(
                representative_id,
                (group_id, line),
            )
            if representative_group != group_id:
                representative = line
            if representative not in result[group_id]:
                result[group_id].append(representative)
        return {
            group_id: "\n".join(values)
            for group_id, values in result.items()
        }


def _failure_issue_refs(group: SemanticGroup) -> List[str]:
    text = "\n".join(
        [
            group.comments,
            group.internal_comments,
            group.cause_evidence,
            *group.redmine_urls,
        ]
    )
    references = {
        "redmine:" + issue_id
        for issue_id in extract_redmine_issue_ids(text)
    }
    references.update(
        match.group(0).casefold()
        for match in _INTERNAL_CHANGE_URL.finditer(text)
    )
    return sorted(references)


def _failure_evidence_chunks(value: object) -> List[str]:
    text = str(value or "").replace("#012", "\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    segments: List[str] = []
    for raw_line in text.splitlines():
        line = _SPACE.sub(" ", raw_line).strip()
        if not line or not _FAILURE_SIGNAL.search(line):
            continue
        if len(line) <= 6000:
            segments.append(line)
            continue
        matches = list(_FAILURE_SIGNAL.finditer(line))
        for match in matches[-12:]:
            start = max(0, match.start() - 300)
            end = min(len(line), match.end() + 2700)
            segments.append(line[start:end])
        segments.append(line[-3500:])

    unique_segments = []
    for segment in segments:
        normalized = segment.strip()
        if normalized and normalized not in unique_segments:
            unique_segments.append(normalized)

    chunks: List[str] = []
    current: List[str] = []
    characters = 0
    for segment in unique_segments:
        pieces = _overlapping_text_chunks(segment, 8000, 500)
        for piece in pieces:
            added = len(piece) + (1 if current else 0)
            if current and characters + added > 8000:
                chunks.append("\n".join(current))
                current = []
                characters = 0
            current.append(piece)
            characters += len(piece) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _overlapping_text_chunks(
    value: str,
    size: int,
    overlap: int,
) -> List[str]:
    if len(value) <= size:
        return [value]
    result = []
    offset = 0
    while offset < len(value):
        result.append(value[offset: offset + size])
        if offset + size >= len(value):
            break
        offset += size - overlap
    return result


def _failure_evidence_batches(
    records: Sequence[Tuple[str, str, str]],
) -> Iterable[List[Tuple[str, str, str]]]:
    batch: List[Tuple[str, str, str]] = []
    characters = 0
    for record in records:
        size = len(record[2])
        if batch and (
            len(batch) >= 16 or
            characters + size > _MAX_FAILURE_EVIDENCE_BATCH_CHARS
        ):
            yield batch
            batch = []
            characters = 0
        batch.append(record)
        characters += size
    if batch:
        yield batch


def _strict_json_mapping(
    response: Any,
    requested_ids: Sequence[str],
    value_label: str,
) -> Mapping[str, str]:
    try:
        text = str(response.output_text or "").strip()
        fenced = re.fullmatch(
            r"```(?:json)?\s*(\{.*\})\s*```",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fenced:
            text = fenced.group(1)
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("AI response is not strict JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("AI response must be a JSON object")
    if set(parsed) != set(requested_ids):
        raise ValueError("AI response group IDs do not match the request")
    if any(not isinstance(parsed[group_id], str) for group_id in requested_ids):
        raise ValueError("AI {} must be JSON strings".format(value_label))
    return parsed


def _validate_error_representatives(
    parsed: Mapping[str, str],
    requested_ids: Sequence[str],
    groups_by_id: Optional[Mapping[str, str]] = None,
) -> None:
    positions = {
        line_id: index
        for index, line_id in enumerate(requested_ids)
    }
    for line_id in requested_ids:
        representative = parsed[line_id]
        if representative not in positions:
            raise ValueError(
                "AI error representative is not a supplied line_id"
            )
        if positions[representative] > positions[line_id]:
            raise ValueError(
                "AI error representative must not point forward"
            )
        if (
            groups_by_id is not None and
            groups_by_id.get(representative) != groups_by_id.get(line_id)
        ):
            raise ValueError(
                "AI error representative must stay within one ticket group"
            )
    for representative in set(parsed.values()):
        if parsed[representative] != representative:
            raise ValueError(
                "AI error representative must map to itself"
            )


def _generalize_process_variants(
    values: Mapping[str, str],
) -> Dict[str, str]:
    by_suffix: Dict[str, List[Tuple[str, str, re.Match]]] = {}
    for line_id, value in values.items():
        match = _ERROR_PROCESS.match(value)
        if match is None:
            continue
        by_suffix.setdefault(match.group(3).casefold(), []).append(
            (line_id, value, match)
        )
    result = dict(values)
    for entries in by_suffix.values():
        processes = {
            match.group(2).casefold()
            for _line_id, _value, match in entries
        }
        if len(processes) <= 1:
            continue
        for line_id, _value, match in entries:
            result[line_id] = "{}<Process>{}".format(
                match.group(1),
                match.group(3),
            )
    return result


def _public_link_evidence(
    url: str,
    context: object,
    redactor: SetupRedactor,
) -> Mapping[str, Any]:
    if not isinstance(context, Mapping):
        return {
            "url": url,
            "metadata_available": False,
        }
    return {
        "url": url,
        "metadata_available": True,
        "kind": normalize_text(context.get("kind")),
        "title": _sanitize_redmine_link_evidence(
            context.get("title"),
            redactor,
        ),
        "body": _sanitize_redmine_link_evidence(
            context.get("body"),
            redactor,
        ),
        "state": normalize_text(context.get("state")),
        "merged": context.get("merged") is True,
        "labels": [
            _sanitize_redmine_link_evidence(value, redactor)
            for value in context.get("labels") or ()
            if normalize_text(value)
        ],
    }


def _sanitize_redmine_link_evidence(
    value: object,
    redactor: SetupRedactor,
) -> str:
    text = redactor.redact(str(value or ""))
    text = _REDMINE_URL.sub("", text)
    text = _TICKET_ID.sub("internal issue", text)
    text = _REDMINE_REFERENCE.sub("internal issue", text)
    text = _HASH_TICKET_ID.sub("", text)
    text = _EMAIL.sub("<User>", text)
    text = "\n".join(
        _SPACE.sub(" ", line).strip()
        for line in text.splitlines()
        if _SPACE.sub(" ", line).strip()
    )
    redactor.assert_clean([text], "Redmine link evidence")
    return text


def sanitize_ai_summary(
    summary: object,
    group: SemanticGroup,
    redactor: SetupRedactor,
) -> str:
    text = redactor.redact(normalize_text(summary))
    ticket_ids = set()
    change_numbers = set()
    source_values: Iterable[str] = (
        group.comments,
        group.internal_comments,
        group.cause_evidence,
        *group.ticket_refs,
        *group.redmine_urls,
    )
    for value in source_values:
        source = str(value or "")
        ticket_ids.update(_TICKET_ID.findall(source))
        ticket_ids.update(_HASH_TICKET_ID.findall(source))
        change_numbers.update(_INTERNAL_CHANGE_URL.findall(source))
    text = _REDMINE_URL.sub("", text)
    text = _INTERNAL_CHANGE_URL.sub("", text)
    text = _INTERNAL_CHANGE_ID.sub("", text)
    text = _COMMIT_HASH.sub("", text)
    text = _REDMINE_REFERENCE.sub("internal issue", text)
    text = _HASH_TICKET_ID.sub("", text)
    for ticket_id in sorted(ticket_ids, key=len, reverse=True):
        text = re.sub(
            r"(?<!\d)#?{}\b".format(re.escape(ticket_id)),
            "",
            text,
        )
    for change_number in sorted(
        change_numbers,
        key=len,
        reverse=True,
    ):
        text = re.sub(
            r"(?<!\d)#?{}\b".format(re.escape(change_number)),
            "",
            text,
        )
    text = _REDMINE_WORD.sub("internal tracker", text)
    text = "\n".join(
        _SPACE.sub(" ", line).strip(" ,;:-")
        for line in text.splitlines()
    ).strip()
    redactor.assert_clean([text], "AI summary")
    if _REDMINE_URL.search(text):
        raise ValueError("AI summary contains a Redmine URL")
    if _INTERNAL_CHANGE_URL.search(text):
        raise ValueError("AI summary contains an internal change URL")
    if any(re.search(r"\b{}\b".format(re.escape(value)), text) for value in ticket_ids):
        raise ValueError("AI summary contains a Redmine ticket number")
    return text


def extract_error_log_lines(value: object) -> str:
    """Retain complete source ERR lines, including host and timestamp prefixes."""

    text = _normalize_error_log_lines(value)
    result: List[str] = []
    seen = set()
    for raw_line in text.splitlines():
        line = _BULLET.sub("", raw_line)
        line = _SPACE.sub(" ", line).strip()
        severity = _ERR_SEVERITY.search(line)
        if not severity:
            continue
        line = _TRAILING_TICKET_PROSE.sub("", line).rstrip()
        if not _ERROR_LOG_LINE.match(line[severity.start():]):
            continue
        key = line.casefold()
        if key not in seen:
            seen.add(key)
            result.append(line)
    return "\n".join(result)


def generalize_error_logs(value: object) -> str:
    """Build internal signatures while preserving source lines for display."""

    result: List[str] = []
    seen = set()
    for source_line in extract_error_log_lines(value).splitlines():
        severity = _ERR_SEVERITY.search(source_line)
        if not severity:
            continue
        line = source_line[severity.start():]
        line = _EMAIL.sub("<User>", line)
        line = _UUID.sub("<ID>", line)
        line = _MAC_ADDRESS.sub("<MAC>", line)
        line = _IPV6_CANDIDATE.sub(_replace_ipv6, line)
        line = _IPV4.sub("x.x.x.x", line)
        primary, marker, continuation = line.partition("#012")
        primary = _semanticize_dynamic_values(primary)
        line = (
            primary + marker + continuation
            if marker
            else primary
        )
        key = line.casefold()
        if key not in seen:
            seen.add(key)
            result.append(line)
    return "\n".join(result)


def error_log_signature(value: object) -> str:
    """Return a stable grouping key for structurally equivalent ERR lines."""

    return generalize_error_logs(value)


def _semanticize_dynamic_values(line: str) -> str:
    line = re.sub(
        r"(?i)(returned value\s+)"
        r"(?:<Value>|[-+]?(?:0x[0-9a-f]+|\d+))",
        r"\1<ReturnedValue>",
        line,
    )
    line = re.sub(
        r"(?i)(\bclient_pid=)(?:<Value>|[-+]?\d+)",
        r"\1<PID>",
        line,
    )
    line = re.sub(
        r"(?i)(lag by log id\s+)(?:<Value>|[-+]?\d+)",
        r"\1<LAG_ID>",
        line,
    )
    line = re.sub(
        r"(?i)(\b(?:OID|oid):)(?:<Value>|0x[0-9a-f]+|\d+)",
        r"\1<OID>",
        line,
    )
    line = re.sub(
        r"(?i)(\./[A-Za-z0-9_./-]+\.c)"
        r"\[(?:<Value>|\d+)\]",
        r"\1[<Line>]",
        line,
    )
    line = re.sub(
        r"(?i)\bEthernet(?:<Value>|\d+)(?!\w)",
        "<Interface>",
        line,
    )
    line = re.sub(
        r"(?i)\b(?:asic|ASIC)(?:<Value>|\d+)(?!\w)",
        lambda match: match.group(0)[:4] + "<ASIC_ID>",
        line,
    )
    line = re.sub(
        r"(?i)(x\.x\.x\.x|<IPv6>):(?:<Value>|\d+)",
        r"\1:<Port>",
        line,
    )
    line = re.sub(
        r"(?i)(Failed Get #)(?:<Value>|\d+)",
        r"\1<AttributeIndex>",
        line,
    )
    line = re.sub(
        r"(?i)(session-id\s+)(?:<Value>|\d+)",
        r"\1<SessionID>",
        line,
    )
    line = re.sub(
        r"(?i)(sx_bridge_id:)(?:<Value>|\d+)",
        r"\1<BridgeID>",
        line,
    )
    line = re.sub(
        r"(?i)(log_port:)(?:<Value>|\d+)",
        r"\1<PortID>",
        line,
    )
    line = re.sub(
        r"(?i)\bv(?:<Value>|\d+(?:\.\d+)+)(?!\w)",
        "<Version>",
        line,
    )
    line = re.sub(
        r"(?i)(/e/)(?:<Value>|\d+)",
        r"\1<ErrorCode>",
        line,
    )
    line = re.sub(
        r"^(ERR(?:OR)?\s+[^:\s]+)"
        r"\[(?:<Value>|\d+)\]",
        r"\1[<PID>]",
        line,
        flags=re.IGNORECASE,
    )

    def replace_number(match: re.Match) -> str:
        prefix = line[max(0, match.start() - 40): match.start()].casefold()
        if match.group(0).casefold() == "<value>":
            return "<RuntimeNumber>"
        if "error code" in prefix or "librelp error" in prefix:
            return "<ErrorCode>"
        if re.search(r"\b(?:on\s+)?port\s*$", prefix):
            return "<Interface>"
        if re.search(r"(?:^|[_\s-])id[:=\s]*$", prefix):
            return "<ID>"
        return match.group(0)

    return _DYNAMIC_NUMBER.sub(replace_number, line)


def _replace_ipv6(match: re.Match) -> str:
    try:
        value = ipaddress.ip_address(match.group(0))
    except ValueError:
        return match.group(0)
    return "<IPv6>" if value.version == 6 else match.group(0)


def sanitize_error_logs(
    value: object,
    group: SemanticGroup,
    redactor: SetupRedactor,
) -> str:
    """Remove setup names while preserving all other original ERR values."""

    del group
    text = redactor.redact(str(value or ""))
    result = extract_error_log_lines(text)
    redactor.assert_clean([result], "AI error logs")
    if _DISPLAY_ERROR_PLACEHOLDER.search(result):
        raise ValueError("AI error logs contain a display placeholder")
    return result


def deduplicate_error_logs(
    value: object,
    group: SemanticGroup,
    redactor: SetupRedactor,
) -> str:
    """Keep the first source line for each deterministic fallback signature."""

    result: List[str] = []
    seen = set()
    for line in sanitize_error_logs(value, group, redactor).splitlines():
        signature = normalize_text(error_log_signature(line)).casefold()
        if signature and signature not in seen:
            seen.add(signature)
            result.append(line)
    return "\n".join(result)


def _structural_error_representatives(
    records: Sequence[Tuple[str, str, str]],
) -> Tuple[
    List[Tuple[str, str, str]],
    Dict[str, str],
]:
    signatures = _generalize_process_variants({
        line_id: error_log_signature(line)
        for line_id, _group_id, line in records
    })
    first_by_signature: Dict[Tuple[str, str], str] = {}
    representative_by_line: Dict[str, str] = {}
    representative_records: List[Tuple[str, str, str]] = []
    for record in records:
        line_id, group_id, _line = record
        signature = normalize_text(signatures.get(line_id)).casefold()
        if not signature:
            signature = "unique:{}".format(line_id)
        representative = first_by_signature.setdefault(
            (group_id, signature),
            line_id,
        )
        representative_by_line[line_id] = representative
        if representative == line_id:
            representative_records.append(record)
    return representative_records, representative_by_line


def _error_pattern_batches(
    records: Sequence[Tuple[str, str, str]],
) -> List[List[Tuple[str, str, str]]]:
    batches: List[List[Tuple[str, str, str]]] = []
    current: List[Tuple[str, str, str]] = []
    current_chars = 0
    for record in records:
        added = len(record[0]) + len(record[2]) + 80
        if (
            current and
            current_chars + added > _MAX_ERROR_PATTERN_BATCH_CHARS
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(record)
        current_chars += added
    if current:
        batches.append(current)
    return batches


def _normalize_error_log_lines(value: object) -> str:
    text = str(value or "")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _unique_json_object(
    pairs: Sequence[Tuple[str, object]],
) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("AI response contains a duplicate JSON key")
        result[key] = value
    return result
