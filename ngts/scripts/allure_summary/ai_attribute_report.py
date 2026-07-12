#!/usr/bin/env python3
"""AI-driven bug attribution for an Allure report.

For each failed/broken test in a published Allure report:
  1. Pre-filter the curated baseline (Redmine query 36102) to candidates
     plausibly relevant (test-name overlap, setup overlap, error-pattern hit).
  2. Ask the LLM to pick the single best candidate or "none".
  3. Build AI-only mappings, re-render the report to <source>-ai-mapped.

No human in the loop. Designed to run from a MARS step or cron. Always
non-fatal: no key -> warn + exit 0, HTTP/parse errors -> exit 0.

Usage:
    INFERENCE_HUB_API_KEY=<key> python ai_attribute_report.py \
        <source_url> [--setup <name>] [--max-failures N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import uuid
from typing import Optional

ALLURE_BASE = "http://allure.nvidia.com/allure-docker-service"
BASELINE_PATH = "/auto/sw_system_project/NVOS_INFRA/bug_attribution/known_bugs_baseline.json"
CURATED_PATH = "/auto/sw_system_project/NVOS_INFRA/bug_attribution/known_bugs_mappings.json"
CACHE_PATH = "/auto/sw_system_project/NVOS_INFRA/bug_attribution/ai_known_pairings.json"

# Only LLM verdicts with confidence >= this get cached. Lower-conf picks
# are revisited every run so the agent doesn't lock in a shaky decision.
CACHE_MIN_CONFIDENCE = 0.95

# Patterns to extract the defect token the LLM cited in its reason.
# The agent's reasons consistently quote the shared phrase; we use it as
# the cache key so the entry is stable across hostnames/IPs.
_DEFECT_TOKEN_PATTERNS = [
    re.compile(r"defect token ['\"]([^'\"\n]{4,120})['\"]"),
    re.compile(r"matches? the defect token ['\"]([^'\"\n]{4,120})['\"]"),
    re.compile(r"failure (?:message|symptom) ['\"]([^'\"\n]{4,120})['\"]"),
    re.compile(r"failure (?:contains|matches|has) ['\"]([^'\"\n]{4,120})['\"]"),
    re.compile(r"['\"]([^'\"\n]{6,100})['\"] match(?:es)? (?:the|both)"),
]
# Strip variable hostname/IP tokens before storing - keeps the substring
# comparable across reports from different setups in the same family.
_HOSTNAME_NOISE = re.compile(
    r"<SonicHost\s+[\w\d.-]+>"
    r"|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
    r"|\b(?:croc-|mamba-|taipan-)[\w\d-]+\b",
    re.IGNORECASE,
)

# Reuse production helpers. The script lives at
# <repo>/ngts/scripts/allure_summary/ai_attribute_report.py - derive the repo
# root from __file__ so this works under any user (developer / MARS / cron).
_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
sys.path.insert(0, _REPO_ROOT)
from ngts.scripts.allure_summary.bug_marker import (
    REDMINE_ISSUE_URL,
    KNOWN_BUG_TAG, KNOWN_BUG_TAG_PREFIX_REDMINE, REJECTED_BUG_TAG,
    KNOWN_BUG_SENTINEL_PREFIX, KNOWN_BUG_SENTINEL_SUFFIX,
    REJECTED_BUG_SENTINEL_PREFIX, REJECTED_BUG_SENTINEL_SUFFIX,
    NO_KNOWN_BUG_SENTINEL, KNOWN_BUG_CATEGORIES,
    SYSTEM_TYPE_ALIASES,
    score_failure_against_baseline,
    strip_prior_sentinels,
)
from ngts.scripts.allure_summary.llm_client import LLMGatewayClient


# ----------------------------- Allure HTTP --------------------------------

def http_get_json(url: str, timeout: int = 15) -> Optional[dict]:
    """Fetch URL and decode JSON. Internal Allure server: cert verification
    sometimes fails inside MARS docker; fall back to an unverified context.
    Errors print to STDOUT (not stderr) so they appear in MARS step logs."""
    import ssl

    def _try(u: str) -> Optional[dict]:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "ai-attr/1"})
            ctx = ssl.create_default_context()
            try:
                # noqa - internal allure server, no untrusted input
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except ssl.SSLError:
                # Internal Allure - retry without cert verification
                # (internal CA, no untrusted input)
                ctx = ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                    return json.loads(r.read())
        except Exception as exc:
            print(f"  http error ({u}): {exc}")
            return None
    res = _try(url)
    if res is not None:
        return res
    # Fall back from https -> http for internal Allure when TLS fails entirely.
    if url.startswith("https://"):
        alt = "http://" + url[len("https://"):]
        print(f"  retrying as http: {alt}")
        return _try(alt)
    return None


def parse_source_url(url: str) -> tuple[str, str, int]:
    url = url.rstrip("/")
    if url.endswith("/index.html"):
        url = url[:-len("/index.html")]
    parts = url.split("/")
    project = parts[parts.index("projects") + 1]
    report_id = int(parts[parts.index("reports") + 1])
    return url, project, report_id


def derive_setup_from_project(project: str) -> str:
    p = project.lower().replace("-session-reports", "")
    if p.startswith("nvos-"):
        p = p[len("nvos-"):]
    for canon, aliases in SYSTEM_TYPE_ALIASES.items():
        for a in aliases:
            if p.startswith(a + "-") or p == a:
                return canon
    return p


def fetch_all_testcases(base: str) -> list[dict]:
    suites = http_get_json(f"{base}/data/suites.json")
    if not suites:
        return []
    leaves: list = []

    def walk(node):
        for c in node.get("children", []) or []:
            if "children" in c:
                walk(c)
            elif c.get("status"):
                leaves.append(c)
    walk(suites)
    out = []
    for leaf in leaves:
        uid = leaf.get("uid")
        if uid:
            tc = http_get_json(f"{base}/data/test-cases/{uid}.json", timeout=20)
            if tc:
                out.append(tc)
    return out


# ---------------------------- Candidate pool ------------------------------

_PARAM_RE = re.compile(r"\[.*\]$")


def _setup_matches(setups, setup_name: str) -> bool:
    if not setups:
        return True
    if isinstance(setups, str):
        setups = [setups]
    name_lc = setup_name.lower()
    for s in setups:
        s_lc = (s or "").strip().lower()
        if s_lc in name_lc:
            return True
        for alias in SYSTEM_TYPE_ALIASES.get(s_lc, []):
            if alias.lower() in name_lc:
                return True
    return False


_STOPWORDS = {
    "test", "the", "and", "for", "with", "from", "this", "that", "have",
    "after", "before", "when", "while", "then", "into", "over", "under",
    "true", "false", "none", "null", "fail", "failed", "error", "errors",
    "warning", "assert", "exception", "traceback", "result", "expected",
    "actual", "config", "value", "values", "check", "found", "missing",
    "func", "method", "args", "kwargs", "self", "return", "yield",
    # platform/api noise
    "nvue", "openapi", "ngts", "test_", "sonic", "switch", "interface",
}


def _tokens(text: str) -> set:
    """Distinctive lowercase tokens >= 4 chars, not in the stop-word list."""
    if not text:
        return set()
    out = set()
    for tok in re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{3,}", text):
        t = tok.lower()
        if t in _STOPWORDS:
            continue
        out.add(t)
    return out


def pick_candidates(failure: dict, baseline: dict, setup: str, k: int = 6,
                    loose: bool = False) -> list[dict]:
    """Pre-filter baseline bugs to candidates worth showing the LLM.

    Tiers (most specific first):
      1. test name exact/prefix + setup match
      2. test name exact/prefix, any setup
      3. error_pattern substring hit + setup match
    With `loose=True`, two more tiers are appended:
      4. shared distinctive tokens between test_name and bug subject/description
      5. shared distinctive tokens between failure message and bug subject
    Cap at `k` candidates to keep prompts short (defaults to 6 strict, 10 loose).
    """
    if loose and k <= 6:
        k = 10

    name = failure.get("name") or ""
    base_name = _PARAM_RE.sub("", name)
    haystack = ((failure.get("statusMessage") or "") + "\n" +
                (failure.get("statusTrace") or "")).lower()

    test_tokens = _tokens(base_name)
    msg_tokens = _tokens(failure.get("statusMessage") or "")

    tier1: list = []
    tier2: list = []
    tier3: list = []
    tier4: list = []  # subject token overlap with test name (loose only)
    tier5: list = []  # subject token overlap with failure message (loose only)
    seen_ids: set = set()

    for bug in baseline.get("bugs", []) or []:
        rid = bug.get("redmine_id")
        if rid in seen_ids:
            continue
        tests = bug.get("tests") or []
        setup_ok = _setup_matches(bug.get("setup_filters"), setup)
        name_hit = False
        for t in tests:
            if t == base_name or t == name:
                name_hit = True
                break
            if base_name.startswith(t + "_"):
                name_hit = True
                break
        if name_hit:
            (tier1 if setup_ok else tier2).append(bug)
            seen_ids.add(rid)
            continue
        # Error-pattern hit + setup match
        pat_hit = False
        if setup_ok:
            for pat in bug.get("error_patterns") or []:
                if pat and pat.lower() in haystack:
                    tier3.append(bug)
                    seen_ids.add(rid)
                    pat_hit = True
                    break
        if pat_hit or not loose:
            continue

        # LOOSE tiers below — token-overlap heuristic
        subj_tokens = _tokens(bug.get("subject") or "")
        # Tier 4: test-name shares >=2 distinctive tokens with bug subject
        name_overlap = test_tokens & subj_tokens
        if len(name_overlap) >= 2:
            tier4.append((bug, len(name_overlap)))
            seen_ids.add(rid)
            continue
        # Tier 5: failure-message shares >=3 distinctive tokens with subject
        msg_overlap = msg_tokens & subj_tokens
        if len(msg_overlap) >= 3:
            tier5.append((bug, len(msg_overlap)))
            seen_ids.add(rid)

    # Sort token-overlap tiers by overlap count (descending) so most-similar
    # bugs rise first, then drop the score.
    tier4_sorted = [b for b, _ in sorted(tier4, key=lambda x: -x[1])]
    tier5_sorted = [b for b, _ in sorted(tier5, key=lambda x: -x[1])]

    return (tier1 + tier2 + tier3 + tier4_sorted + tier5_sorted)[:k]


# -------------------------------- LLM -------------------------------------

LLM_SYSTEM_BASE = (
    "You are an NVOS regression triage assistant. You receive a failing pytest "
    "test name, an excerpt of its failure message + stack trace, the switch "
    "platform it ran on, and a short list of candidate open Redmine bugs from "
    "the verification team's known-bugs database. "
    "Pick the single bug that BEST explains this specific failure, or 'none' "
    "if the failure does not match any candidate convincingly. "
    "Reply with strict JSON: "
    "{\"redmine_id\": <int|null>, \"confidence\": <0.0-1.0>, \"reason\": \"<one sentence>\"}. "
    "Be conservative: when in doubt, prefer 'none' over a forced match. "
    "Cross-platform speculation is not enough - the bug subject and the "
    "failure must describe the same defect symptom."
)


def load_cache(path: str = CACHE_PATH) -> dict:
    """Load the AI-maintained pairings cache. Returns an empty shell if absent."""
    default = {
        "_meta": {
            "maintained_by": "ai_attribute_report.py",
            "purpose": "AI-maintained cache of high-confidence (test, bug) pairings. "
                       "Humans may inspect / delete entries; AI repopulates on next run.",
            "schema_version": 1,
        },
        "pairings": [],
    }
    if not os.path.exists(path):
        return default
    try:
        with open(path) as fh:
            doc = json.load(fh)
        if not isinstance(doc.get("pairings"), list):
            return default
        return doc
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARN: cache at {path} unreadable: {exc}", file=sys.stderr)
        return default


def save_cache(cache: dict, path: str = CACHE_PATH) -> None:
    """Write the cache atomically. No-op on permission errors."""
    import datetime as _dt
    cache.setdefault("_meta", {})["updated_at"] = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cache, fh, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"WARN: cache write to {path} failed: {exc}", file=sys.stderr)


def _normalize_token(token: str) -> str:
    if not token:
        return ""
    return _HOSTNAME_NOISE.sub("", token).strip()


def extract_defect_token(llm_reason: str) -> Optional[str]:
    """Pull the cited defect token out of the LLM's reason string."""
    if not llm_reason:
        return None
    for rx in _DEFECT_TOKEN_PATTERNS:
        m = rx.search(llm_reason)
        if m:
            tok = _normalize_token(m.group(1))
            if 4 <= len(tok) <= 120:
                return tok
    return None


def _setup_matches_cache(entry_setup: str, run_setup: str) -> bool:
    """Same alias-aware match the curated matcher uses."""
    if not entry_setup or not run_setup:
        return True
    a = entry_setup.lower()
    b = run_setup.lower()
    if a in b or b in a:
        return True
    for canon, aliases in SYSTEM_TYPE_ALIASES.items():
        canon_in_a = (a == canon or any(al in a for al in aliases))
        canon_in_b = (b == canon or any(al in b for al in aliases))
        if canon_in_a and canon_in_b:
            return True
    return False


def cache_lookup(cache: dict, setup: str, test_name: str, status_message: str) -> Optional[dict]:
    """Return the best-matching cache entry (highest confidence) or None.

    A hit requires: alias-aware setup match + exact test_name match (including
    [...] suffix) + defect_token is case-insensitive substring of the failure
    message. Same strict-AND model as the curated matcher.
    """
    msg = (status_message or "").lower()
    if not test_name or not msg:
        return None
    best = None
    for entry in cache.get("pairings", []) or []:
        if entry.get("test_name") != test_name:
            continue
        token = (entry.get("defect_token") or "").lower()
        if not token or token not in msg:
            continue
        if not _setup_matches_cache(entry.get("system_type", ""), setup):
            continue
        if best is None or (entry.get("confidence") or 0) > (best.get("confidence") or 0):
            best = entry
    return best


def update_cache(cache: dict, setup: str, test_name: str, defect_token: str,
                 redmine_id: int, confidence: float, reason: str) -> bool:
    """Insert a new entry or bump hits on an existing one. Returns True if changed."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pairings = cache.setdefault("pairings", [])
    for entry in pairings:
        if (entry.get("test_name") == test_name and
                entry.get("redmine_id") == redmine_id and
                _setup_matches_cache(entry.get("system_type", ""), setup)):
            entry["last_seen"] = now
            entry["hits"] = (entry.get("hits") or 0) + 1
            entry["confidence"] = max(entry.get("confidence") or 0, confidence)
            return True
    pairings.append({
        "system_type": setup,
        "test_name": test_name,
        "defect_token": defect_token,
        "redmine_id": redmine_id,
        "confidence": confidence,
        "first_seen": now,
        "last_seen": now,
        "hits": 1,
        "source_reason": (reason or "")[:240],
    })
    return True


def load_feedback(path: Optional[str]) -> dict:
    """Load human-editable feedback file. Schema documented in the file itself."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARN: feedback file unreadable: {exc}", file=sys.stderr)
        return {}


def build_system_prompt(feedback: dict) -> str:
    """Compose system prompt by appending human-supplied rules + patterns.

    Two channels are read from the feedback file:
      - system_prompt_addendum: free-text rules, appended verbatim.
      - pattern_lessons: abstract failure-mode patterns (no specific test
        names or bug IDs). Each is formatted as a four-bullet block so the
        LLM treats them as transferable heuristics, not lookup entries.

    The legacy `example_corrections` channel (specific test->bug examples)
    is still tolerated for backward compatibility but should be considered
    deprecated - specific ground truth belongs in known_bugs_mappings.json.
    """
    parts = [LLM_SYSTEM_BASE]

    extra = (feedback or {}).get("system_prompt_addendum") or ""
    if extra.strip():
        parts.append("Additional rules from the regression team:\n" + extra.strip())

    patterns = (feedback or {}).get("pattern_lessons") or []
    if patterns:
        lines = [
            "DECISION CHECKLIST — for each candidate bug, walk these checks IN ORDER. "
            "The first check that fires determines the outcome. After processing all "
            "candidates, pick the strongest surviving attribute call, or return null. "
            "The 'illustration' lines use FAKE test names and FAKE bug IDs - they are "
            "worked examples, not lookups. Apply the pattern's heuristic, not the "
            "literal example."
        ]
        for p in patterns[:12]:
            block = [
                "",
                f"{p.get('pattern', '')}",
                f"  Trigger        : {p.get('trigger', '')}",
                f"  Common mistake : {p.get('common_mistake', '')}",
                f"  Heuristic      : {p.get('correct_heuristic', '')}",
            ]
            ill = p.get("illustration")
            if ill:
                block.append(f"  Worked example : {ill}")
            lines.append("\n".join(block))
        parts.append("\n".join(lines))

    # Backward compat - older feedback files used example_corrections.
    legacy = (feedback or {}).get("example_corrections") or []
    if legacy:
        ex_lines = ["DEPRECATED specific examples (treat as abstract patterns, not lookups):"]
        for i, e in enumerate(legacy[:6], 1):
            ex_lines.append(
                f"  {i}. lesson: {e.get('lesson', '')}"
            )
        parts.append("\n".join(ex_lines))

    return "\n\n".join(parts)


def build_user_msg(failure: dict, setup: str, candidates: list[dict]) -> str:
    err = (failure.get("statusMessage") or "")[:600]
    trace = (failure.get("statusTrace") or "")[:400]
    candidate_lines = []
    for i, b in enumerate(candidates, 1):
        subj = (b.get("subject") or "")[:160]
        setups = b.get("setup_filters") or []
        tests = b.get("tests") or []
        eps = b.get("error_patterns") or []
        kind = b.get("kind") or "unknown"
        candidate_lines.append(
            f"  [{i}] Redmine #{b['redmine_id']}\n"
            f"      kind    : {kind}\n"
            f"      subject : {subj}\n"
            f"      setups  : {setups}\n"
            f"      tests   : {tests[:5]}\n"
            f"      err_pat : {[e[:80] for e in eps[:3]]}"
        )
    candidate_str = "\n".join(candidate_lines) if candidate_lines else "  (none)"
    return (
        f"Test: {failure.get('name')}\n"
        f"Platform: {setup}\n"
        f"Status: {failure.get('status')}\n"
        f"\nstatusMessage:\n{err}\n"
        f"\nstatusTrace (head):\n{trace}\n"
        f"\nCandidate bugs:\n{candidate_str}\n"
        f"\nReturn JSON only."
    )


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def parse_llm_verdict(raw: Optional[str]) -> dict:
    if not raw:
        return {"redmine_id": None, "confidence": 0.0, "reason": "empty response"}
    m = _JSON_RE.search(raw)
    if not m:
        return {"redmine_id": None, "confidence": 0.0, "reason": "no JSON in response"}
    try:
        v = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"redmine_id": None, "confidence": 0.0, "reason": "invalid JSON"}
    if not isinstance(v, dict):
        return {"redmine_id": None, "confidence": 0.0, "reason": "JSON was not an object"}
    rid = v.get("redmine_id")
    if rid is not None:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            rid = None
    try:
        conf = float(v.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    # Clamp into [0.0, 1.0]; the LLM occasionally emits 1.5 or -0.2.
    conf = max(0.0, min(1.0, conf))
    return {
        "redmine_id": rid,
        "confidence": conf,
        "reason": str(v.get("reason") or "")[:240],
    }


# -------------------------- Re-render result.json --------------------------

# Pytest's traceback formatter renders '???' instead of source lines when its
# AST-based statement-range matcher can't resolve the enclosing statement -
# typically multi-line function calls with f-string kwargs inside nested
# with/for blocks. The frame still has correct file:line info but the body is
# useless. We scrub these frames from the re-rendered AI report so the trace
# panel doesn't show noise.
# Separator line: pytest emits a row of underscores possibly interleaved with
# spaces and an optional trailing space. Match any line whose visible content
# is just spaces and underscores AND contains at least one underscore.
_FRAME_SEP_RE = re.compile(r"\n[ _]*_[ _]*\n")
_QMARK_LINE_RE = re.compile(r"^\s*>?\s*\?\?\?\s*$", re.MULTILINE)
_FRAME_SEP_JOIN = "\n" + "_ " * 39 + "_ \n"


def _scrub_question_mark_frames(trace: str) -> str:
    """Drop pytest traceback frames whose source rendered as '???'.
    Preserves the rest of the trace verbatim (other frames, separators,
    locals dumps in real frames). Returns input unchanged if no '???'
    pattern is found.
    """
    if not trace or "???" not in trace:
        return trace
    parts = _FRAME_SEP_RE.split(trace)
    keep = [p for p in parts if not _QMARK_LINE_RE.search(p)]
    return _FRAME_SEP_JOIN.join(keep)


def _translate_step(server_step: dict) -> dict:
    """Map an Allure-server testStage step into result.json step shape.
    Drops attachments (the source files live in the original report's data
    dir and are not copied into the re-rendered alluredir, so referencing
    them would render as broken icons in the UI).
    """
    time = server_step.get("time") or {}
    msg = server_step.get("statusMessage") or ""
    trace = _scrub_question_mark_frames(server_step.get("statusTrace") or "")
    details: dict = {}
    if msg:
        details["message"] = msg
    if trace:
        details["trace"] = trace
    out = {
        "name": server_step.get("name") or "",
        "status": server_step.get("status") or "passed",
        "stage": "finished",
        "start": time.get("start", 0),
        "stop": time.get("stop", 0),
        "steps": [_translate_step(s) for s in (server_step.get("steps") or [])],
        "attachments": [],
        "parameters": server_step.get("parameters") or [],
    }
    if details:
        out["statusDetails"] = details
    return out


def build_result_json(tc: dict, bug: Optional[dict]) -> dict:
    status = tc.get("status") or "broken"
    is_failure = status in ("failed", "broken")
    labels = list(tc.get("labels", []) or [])
    labels = [l for l in labels if not (
        l.get("name") == "tag" and (l.get("value", "") or "").startswith(("known_bug", "rejected_bug"))
    )]
    links: list = []
    rid_str = ""
    is_rejected = is_failure and bug and (bug.get("status_kind") == "rejected")
    if is_failure and bug:
        rid = bug["redmine_id"]
        subject = (bug.get("subject") or "")[:90]
        suffix = " (AI)" if not is_rejected else " (AI, REJECTED bug - test may need update)"
        link_name = f"Redmine #{rid}" + (f" - {subject}" if subject else "") + suffix
        links.append({
            "type": "issue",
            "url": REDMINE_ISSUE_URL.format(id=rid),
            "name": link_name,
        })
        top_tag = REJECTED_BUG_TAG if is_rejected else KNOWN_BUG_TAG
        for tag in (top_tag, f"{KNOWN_BUG_TAG_PREFIX_REDMINE}{rid}", "ai_attributed"):
            labels.append({"name": "tag", "value": tag})
        rid_str = f"#{rid}"

    # Strip any sentinel that may have been stamped on the source test-case in
    # an earlier re-render pass - otherwise a refreshed attribution (or a
    # refusal) would stack underneath the stale one and the test would show
    # two contradictory sentinels.
    msg_in = strip_prior_sentinels((tc.get("statusMessage") or "").rstrip())
    trace_in = _scrub_question_mark_frames(tc.get("statusTrace") or "")
    if is_failure:
        if bug and is_rejected:
            sentinel = f"{REJECTED_BUG_SENTINEL_PREFIX}{rid_str}{REJECTED_BUG_SENTINEL_SUFFIX}"
        elif bug:
            sentinel = f"{KNOWN_BUG_SENTINEL_PREFIX}{rid_str}{KNOWN_BUG_SENTINEL_SUFFIX}"
        else:
            sentinel = NO_KNOWN_BUG_SENTINEL
        new_msg = (msg_in + ("\n\n" if msg_in else "") + sentinel).strip()
        details = {"message": new_msg, "trace": trace_in}
    else:
        details = {"message": msg_in or "", "trace": trace_in}

    # Preserve the testStage step tree so the AI-mapped report's UI shows the
    # same "Validate component <X> with default log level <Y>" expandable
    # steps that the source report has - without this the UI shows a flat
    # statusMessage + statusTrace and triage has to read pytest's locals dump
    # to discover which step actually failed.
    server_steps = ((tc.get("testStage") or {}).get("steps")) or []
    steps = [_translate_step(s) for s in server_steps]

    return {
        "uuid": str(uuid.uuid4()),
        "historyId": tc.get("historyId") or uuid.uuid4().hex,
        "testCaseId": tc.get("testCaseId") or uuid.uuid4().hex,
        "name": tc.get("name") or "",
        "fullName": tc.get("fullName") or tc.get("name") or "",
        "status": status,
        "statusDetails": details,
        "stage": "finished",
        "start": (tc.get("time") or {}).get("start", 0),
        "stop": (tc.get("time") or {}).get("stop", 0),
        "labels": labels,
        "links": links,
        "parameters": tc.get("parameters") or [],
        "steps": steps,
        "attachments": [],
    }


# ------------------------------- Accuracy --------------------------------

def evaluate_vs_curated(ai_attributions: dict, curated: dict, setup: str) -> dict:
    """ai_attributions: {test_name -> redmine_id|None}
       curated mapping: {mappings: [{system_type, test_name, redmine_id}]}
    """
    ground: dict = {}
    for m in curated.get("mappings", []) or []:
        if not _setup_matches(m.get("system_type"), setup):
            continue
        rid = m.get("redmine_id") or m.get("nvbugs_id")
        if rid is None:
            continue
        ground[m["test_name"]] = rid

    tests_in_either = set(ai_attributions) | set(ground)
    tp = fp = fn = tn = 0
    wrong_id = 0
    per_test = []
    for t in sorted(tests_in_either):
        ai = ai_attributions.get(t)
        gt = ground.get(t)
        if ai is None and gt is None:
            tn += 1
            verdict = "TN"
        elif ai is None and gt is not None:
            fn += 1
            verdict = "FN (AI missed)"
        elif ai is not None and gt is None:
            fp += 1
            verdict = "FP (AI invented)"
        elif ai == gt:
            tp += 1
            verdict = "TP (match)"
        else:
            wrong_id += 1
            verdict = f"WRONG ID (AI={ai} truth={gt})"
        per_test.append({"test": t, "ai": ai, "ground_truth": gt, "verdict": verdict})

    precision = tp / (tp + fp + wrong_id) if (tp + fp + wrong_id) else 0.0
    recall = tp / (tp + fn + wrong_id) if (tp + fn + wrong_id) else 0.0
    return {
        "counts": {"TP": tp, "FP": fp, "FN": fn, "TN": tn, "WRONG_ID": wrong_id},
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "per_test": per_test,
    }


# -------------------------------- Main -----------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source_url")
    ap.add_argument("--setup", default=None)
    ap.add_argument("--max-failures", type=int, default=40)
    ap.add_argument("--target-project", default=None)
    ap.add_argument("--alluredir", default="/tmp/allure-results-ai-mapped")
    ap.add_argument("--no-upload", action="store_true",
                    help="Skip Allure upload, just emit JSON + accuracy.")
    ap.add_argument("--loose", action="store_true",
                    help="Use loose pre-filter (token-overlap tiers 4+5). "
                         "Doubles candidate count from 6 to 10.")
    ap.add_argument("--feedback", default=None,
                    help="Path to human-edited ai_feedback.json. Rules + past "
                         "corrections get appended to the LLM system prompt.")
    ap.add_argument("--variant", default="default",
                    help="Variant tag included in session JSON filename. "
                         "e.g. 'baseline', 'loose', 'loose-feedback'.")
    args = ap.parse_args()

    base, project, _ = parse_source_url(args.source_url)
    setup = args.setup or derive_setup_from_project(project)
    target_project = args.target_project or f"{project}-ai-mapped"
    print(f"source : {base}")
    print(f"setup  : {setup}")
    print(f"target : {ALLURE_BASE}/projects/{target_project}")

    if not os.path.exists(BASELINE_PATH):
        print(f"ERROR: baseline missing at {BASELINE_PATH}", file=sys.stderr)
        sys.exit(0)
    try:
        with open(BASELINE_PATH) as f:
            baseline = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: failed to read/parse baseline at {BASELINE_PATH}: {e}",
              file=sys.stderr)
        sys.exit(0)
    print(f"baseline: {len(baseline['bugs'])} bugs")

    if os.path.exists(CURATED_PATH):
        try:
            with open(CURATED_PATH) as f:
                curated = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"WARN: failed to read/parse curated at {CURATED_PATH}: {e}; "
                  f"exiting non-fatal", file=sys.stderr)
            sys.exit(0)
    else:
        curated = {"mappings": []}
    print(f"curated truth: {sum(1 for m in curated['mappings'] if _setup_matches(m.get('system_type'), setup))} entries scoped to {setup}")

    llm = LLMGatewayClient()
    if not llm.is_available():
        print("WARN: no LLM credentials; exiting non-fatal (set INFERENCE_HUB_API_KEY)",
              file=sys.stderr)
        sys.exit(0)
    print(f"llm    : backend={llm.backend} model={llm.model}")

    feedback = load_feedback(args.feedback)
    if feedback:
        print(f"feedback: loaded from {args.feedback} "
              f"(rules={bool(feedback.get('system_prompt_addendum'))}, "
              f"patterns={len(feedback.get('pattern_lessons') or [])}, "
              f"legacy_examples={len(feedback.get('example_corrections') or [])})")
    system_prompt = build_system_prompt(feedback)

    cache = load_cache()
    cache_pairings_in = len(cache.get("pairings", []))
    print(f"cache  : {cache_pairings_in} known pairings loaded from {CACHE_PATH}")

    print("fetching all test-cases...")
    tcs = fetch_all_testcases(base)
    failures = [t for t in tcs if t.get("status") in ("failed", "broken")]
    print(f"  {len(tcs)} total / {len(failures)} failures")

    # AI attribution loop
    ai_attribs: dict = {}
    audit: list = []
    asked = 0
    cache_hits = 0
    cache_new = 0
    for tc in failures:
        if asked >= args.max_failures:
            break
        haystack = ((tc.get("statusMessage") or "") +
                    "\n" + (tc.get("statusTrace") or ""))

        # FAST PATH - cache lookup. Strict-AND match (setup alias, exact
        # test_name including [...], defect_token substring in failure
        # message). Skips pre-filter and LLM entirely on a hit.
        hit = cache_lookup(cache, setup, tc["name"], haystack)
        if hit:
            ai_attribs[tc["name"]] = hit["redmine_id"]
            hit["hits"] = (hit.get("hits") or 0) + 1
            import datetime as _dt
            hit["last_seen"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            audit.append({
                "test": tc["name"],
                "verdict": {
                    "redmine_id": hit["redmine_id"],
                    "confidence": hit.get("confidence") or 0.95,
                    "reason": f"cache hit (defect_token={hit['defect_token']!r}, hits={hit['hits']})",
                },
                "candidates": [hit["redmine_id"]],
                "source": "cache",
            })
            cache_hits += 1
            print(f"  * {tc['name']}  -> {hit['redmine_id']}  (cached, hits={hit['hits']})")
            continue

        candidates = pick_candidates(tc, baseline, setup, loose=args.loose)
        if not candidates:
            ai_attribs[tc["name"]] = None
            audit.append({"test": tc["name"], "verdict": {"redmine_id": None, "confidence": 1.0,
                                                          "reason": "no candidates from baseline"},
                          "candidates": []})
            print(f"  - {tc['name']}  -> None  (no candidates from baseline)")
            continue

        # Pre-LLM deterministic gate. Run the offline matcher's scoring
        # rubric (same as bug_marker.attach_baseline_to_failed_results)
        # against the fresh baseline. If a candidate scores >= 60 - which
        # means the bug's error_pattern hits the assertion text, or the
        # bug names this test in tests[] - attribute deterministically and
        # skip the LLM call. This catches the case where a bug was added
        # to the baseline AFTER the regression's session-finish hook ran,
        # and also short-circuits LLM judgment errors on cases the rubric
        # decides cleanly. Keeps the LLM as a fallback for ambiguous matches.
        det = score_failure_against_baseline(
            test_name=tc.get("name", ""),
            status_message=tc.get("statusMessage", "") or "",
            status_trace=tc.get("statusTrace", "") or "",
            baseline=baseline,
            setup_name=setup,
            min_score=60,
        )
        if det is not None:
            best_bug, score = det
            rid = best_bug.get("redmine_id") or best_bug.get("id")
            ai_attribs[tc["name"]] = rid
            audit.append({
                "test": tc["name"],
                "verdict": {
                    "redmine_id": rid,
                    "confidence": 1.0,
                    "reason": f"deterministic rubric score={score} (offline matcher)",
                },
                "candidates": [c["redmine_id"] for c in candidates],
                "source": "offline",
            })
            print(f"  + {tc['name']}  -> {rid}  (deterministic score={score})")
            continue

        user_msg = build_user_msg(tc, setup, candidates)
        resp = llm.chat_completion(
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_msg}],
            max_tokens=200,
            temperature=0.1,
        )
        v = parse_llm_verdict(resp)
        rid = v["redmine_id"]
        # Sanity: only accept ids the LLM saw in candidates (prevents hallucinated bug numbers)
        if rid is not None and rid not in {c["redmine_id"] for c in candidates}:
            v["reason"] = f"REJECTED hallucinated id {rid}; " + v["reason"]
            rid = None
            v["redmine_id"] = None
        ai_attribs[tc["name"]] = rid
        audit.append({"test": tc["name"], "verdict": v,
                      "candidates": [c["redmine_id"] for c in candidates],
                      "source": "llm"})
        asked += 1
        marker = "+" if rid else "."
        print(f"  {marker} {tc['name']}  -> {rid}  (conf={v['confidence']:.2f}) {v['reason'][:80]}")

        # Cache write: high-confidence picks only. Defect token extracted
        # from the LLM's own reason; if we can't extract one, we don't cache.
        if rid is not None and float(v.get("confidence") or 0) >= CACHE_MIN_CONFIDENCE:
            tok = extract_defect_token(v.get("reason") or "")
            if tok and tok.lower() in haystack.lower():
                if update_cache(cache, setup, tc["name"], tok, rid,
                                float(v["confidence"]), v.get("reason") or ""):
                    cache_new += 1

    save_cache(cache)
    print(f"\ncache: {cache_hits} hits this run, {cache_new} new entries written "
          f"({cache_pairings_in} -> {len(cache.get('pairings', []))} total)")

    # Re-render
    if not args.no_upload:
        os.makedirs(args.alluredir, exist_ok=True)
        for f in os.listdir(args.alluredir):
            os.remove(os.path.join(args.alluredir, f))
        candidate_by_id = {b["redmine_id"]: b for b in baseline["bugs"]}
        for tc in tcs:
            bug = None
            if tc.get("status") in ("failed", "broken"):
                rid = ai_attribs.get(tc.get("name"))
                if rid:
                    bug = candidate_by_id.get(rid)
            result = build_result_json(tc, bug)
            with open(os.path.join(args.alluredir, f"{result['uuid']}-result.json"), "w") as fh:
                json.dump(result, fh)
        with open(os.path.join(args.alluredir, "categories.json"), "w") as fh:
            json.dump(KNOWN_BUG_CATEGORIES, fh, indent=2)

        sys.path.insert(0, os.path.join(_REPO_ROOT, "ngts", "scripts"))
        import allure_reporter as ar
        ar.upload_results(args.alluredir, ALLURE_BASE, target_project)
        ar.generate_report(ALLURE_BASE, target_project)

    # Accuracy vs curated (computed but not blocking; for human review only)
    accuracy = evaluate_vs_curated(ai_attribs, curated, setup)

    # Flag ambiguities for human review next time. Anything with confidence
    # under 0.7, refusals after seeing candidates, or test-name-only candidates
    # that the agent picked.
    ambiguities = []
    for entry in audit:
        v = entry["verdict"]
        c = float(v.get("confidence") or 0.0)
        rid = v.get("redmine_id")
        if not entry["candidates"]:
            continue  # pure baseline gap, not an agent ambiguity
        flag = None
        if rid and c < 0.7:
            flag = "low_confidence_pick"
        elif rid is None and c >= 0.3:
            flag = "deliberate_refusal"
        elif rid is None:
            flag = "weak_refusal"
        if flag:
            ambiguities.append({
                "test": entry["test"],
                "flag": flag,
                "verdict": v,
                "candidates_shown": entry["candidates"],
            })

    # Session file goes to the shared verification dir so the team can edit it
    # and the next run can read what was flagged.
    import datetime as _dt
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_path = (
        f"/auto/sw_system_project/NVOS_INFRA/bug_attribution/"
        f"ai_session_{project}_{args.variant}_{ts}.json"
    )
    legacy_path = f"/tmp/ai_attribution_{project}.json"
    payload = {
        "source": args.source_url,
        "setup": setup,
        "variant": args.variant,
        "loose_pre_filter": args.loose,
        "feedback_file": args.feedback,
        "llm_model": llm.model,
        "ai_attributions": {k: v for k, v in ai_attribs.items()},
        "audit": audit,
        "ambiguities_for_review": ambiguities,
        "accuracy_vs_curated": accuracy,
    }
    for p in (session_path, legacy_path):
        try:
            with open(p, "w") as fh:
                json.dump(payload, fh, indent=2)
        except OSError as exc:
            print(f"WARN: could not write {p}: {exc}", file=sys.stderr)

    print(f"\n=== Accuracy vs human-curated mappings (reference only) ===")
    print(json.dumps(accuracy["counts"], indent=2))
    print(f"precision = {accuracy['precision']}   recall = {accuracy['recall']}")
    print(f"\nambiguities flagged for review: {len(ambiguities)}")
    print(f"session report -> {session_path}")


if __name__ == "__main__":
    main()
