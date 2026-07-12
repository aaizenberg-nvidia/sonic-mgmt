#!/usr/bin/env python3
"""Regenerate ngts/scripts/allure_summary/known_bugs_baseline.json from Redmine.

Single source of truth: Redmine saved query 36102 ("nvos verification open
issues"). For each bug we extract:
    - tests[]          pytest function names mentioned in subject/description
    - error_patterns[] regex over failure message/trace
    - setup_filters[]  switch/platform restrictions (taipan, juliet, ...)
    - status, priority, assigned_to, target_version (for visibility)

The matcher in ngts/scripts/allure_summary/bug_marker.py reads this file at
session start and at finalize time, attaching the matching Redmine bug as a
clickable issue link + tag chip + categories sentinel on every failed test.

Run cadence: weekly via cron (/skill or MARS step). Inputs: REDMINE_API_TOKEN
or MCP credentials. Output: replaces known_bugs_baseline.json in place.

Usage:
    python sync_known_bugs.py [--out PATH] [--from-mcp-cache GLOB]

By default the script calls Redmine REST. With --from-mcp-cache it consumes
already-fetched JSON files (tool-results from MCP) so a Claude session can
regenerate the baseline without API tokens.
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Iterable

# Canonical location is the shared verification dir so every player reads
# the same file. Falls back to an in-tree copy for offline/dev work.
SHARED_OUT = "/auto/sw_system_project/NVOS_INFRA/bug_attribution/known_bugs_baseline.json"
IN_TREE_OUT = os.path.join(os.path.dirname(__file__), "known_bugs_baseline.json")
DEFAULT_OUT = SHARED_OUT

PROJECT_ID = 11028  # NVOS - Design
QUERY_ID = 36102
QUERY_URL = f"https://redmine.mellanox.com/projects/{PROJECT_ID}/issues?query_id={QUERY_ID}"
QUERY_NAME = "all issues for ai"
REDMINE_API_BASE = "https://redmine.mellanox.com"

# pytest test names. Don't use \b -- the char before "test_" is often "_"
# (e.g. "sysdump_test_show_phy_health.tar.gz" appears in pasted log lines
# and that IS the test name). Filter NOISE_TESTS afterwards.
TEST_RE = re.compile(r"test_[a-zA-Z][a-zA-Z0-9_]*")
NOISE_TESTS = {
    "test_path", "test_id", "test_name", "test_case", "test_results",
    "test_log", "test_data", "test_code", "test_step", "test_steps",
    "test_user", "test_password", "test_setup", "test_run",
}

# Platform / switch family vocabulary - match in subject + description.
# Tuple is (regex, normalized-tag). The canonical vocabulary covers the
# platform families NVOS regression actually runs on:
#   - croc     : crocodile family (also Q3200 hwsku, which is crocodile)
#   - bm       : black mamba family - includes Q3400 / QM3400 / Quantum-3,
#                since mtvr-q3400-XX setups ARE black mamba machines (the
#                q3400 in the name denotes which ASIC, not a separate platform)
#   - taipan   : taipan family (also Q3450 hwsku)
#   - juliet   : juliet family (NVL5)
#   - rosalind : rosalind family (NVL6)
# Any other platform mention is ignored - bugs with no matching setup
# token end up with setup_filters=[] (eligible for every setup).
_WB = r"\b"
SETUP_PATTERNS = [
    (re.compile(_WB + r"taipan" + _WB, re.I), "taipan"),
    (re.compile(_WB + r"(?:black ?mamba|white ?mamba|mamba|\bbm\b)" + _WB, re.I), "bm"),
    (re.compile(_WB + r"(?:crocodile|croc[-_]?\d+|crocodile_switch)" + _WB, re.I), "croc"),
    (re.compile(_WB + r"juliet" + _WB, re.I), "juliet"),
    (re.compile(_WB + r"rosalind" + _WB, re.I), "rosalind"),
    # ASIC / hwsku codes - all roll up into one of the platforms above
    (re.compile(_WB + r"Q3200(?:_[A-Z]+)?" + _WB), "croc"),       # Q3200_RA = crocodile
    (re.compile(_WB + r"Q3450(?:_[A-Z]+)?" + _WB), "taipan"),     # Q3450_LD = taipan
    (re.compile(_WB + r"(?:QM3?400|Quantum[- ]?3)" + _WB, re.I), "bm"),  # Q3400/Quantum-3 = bm
]

# Subject prefix tags that classify the bug kind.
LOG_ANALYZER_PREFIX_RE = re.compile(
    r"^\s*(?:Copy of #\d+\s*-\s*)?\[log_analyzer\]\|\s*", re.IGNORECASE
)

# Assertion-like phrases worth surfacing as error_patterns. Tuned to not
# over-match on the literal "AssertionError:" suffix alone.
ASSERTION_RES = [
    re.compile(r"AssertionError:\s*([^\n]{8,200})"),
    re.compile(r"\bExpected:\s*([^\n]{6,160})"),
    re.compile(r"\bExpected\s+([^\n]{6,160})\s+but got\b"),
    re.compile(r"\b(?:Missing|Unexpected)\s+(?:fields?|keys?|element|file)s?:?\s*([^\n]{4,160})"),
    re.compile(r"KeyError:\s*[\"']([^\"'\n]{2,80})[\"']"),
    # Capture the MESSAGE, not the exception class - the class name is
    # non-capturing on purpose. A bug's "Recommended Fix" snippet like
    # `raise ValueError('Invalid file name')` must yield the message, never
    # the bare token "ValueError" (which substring-matches every unrelated
    # failure that raises a ValueError).
    re.compile(r"raise\s+\w+Error\(['\"]([^'\"\n]{6,200})['\"]"),
    re.compile(r"missing\s+['\"]([^'\"\n]{2,80})['\"]"),
    # README convention: bug filers add "Error pattern: <verbatim phrase>"
    # to a bug description to teach the matcher. A '*' is treated as the
    # variable-substring wildcard (e.g. timing values, hostnames); only the
    # literal fragments are extracted so each becomes its own substring
    # pattern.
    re.compile(r"\bError\s+pattern\s*:\s*([^\n]{6,200})", re.IGNORECASE),
]

# Inside an extracted phrase like "['els-input-power', 'oe-lane-temperature']"
# pull each quoted token out as its own pattern - failure messages quote them
# individually, not as a Python list-repr.
QUOTED_TOKEN_RE = re.compile(r"['\"]([A-Za-z][A-Za-z0-9_\-]{2,60})['\"]")
# Same for set-literals shown without quotes: "{'foo', 'bar'}" -> ['foo','bar']
# Already covered by QUOTED_TOKEN_RE.

# Tokens that are too generic to be useful as standalone patterns even though
# they appear quoted in messages. Two categories:
#   1. Universal noise: boolean/null/status words
#   2. Generic NVOS field names: appear quoted in assertions ('status', 'state'
#      etc.) but substring-match hundreds of unrelated failures
# Also includes the NVOS test-framework prefix "*** POSSIBLE BUG ***" which is
# prepended to every command failure output and is useless as a discriminator.
NOISE_TOKENS = {
    # Boolean / null equivalents
    "true", "false", "none", "null", "nan",
    # Binary / tri-state flags
    "yes", "no", "on", "off",
    # Generic status words
    "up", "down", "ok", "fail", "failed", "error", "warning",
    # Generic NVOS field names — appear quoted in assertions but too common
    # to discriminate between unrelated failures
    "status", "state", "value", "result", "reason", "message",
    "type", "name", "data", "code", "info", "detail", "key",
    "output", "input", "response", "request", "version",
    "enabled", "disabled", "active", "config", "mode", "level",
    "timestamp", "time", "id", "flag", "count", "index", "port",
    # NVOS test-framework prefix — prepended to every command error output,
    # appears in virtually every test failure, useless as a discriminator
    "*** possible bug ***", "possible bug",
    # Test name noise
    "test_path", "test_id", "test_name",
}

# Bare exception class names are never useful as standalone error_patterns -
# "ValueError" / "KeyError" substring-match every unrelated failure that
# raises that exception. They leak in from bug "Recommended Fix" code
# snippets (e.g. `raise ValueError('...')`). Dropped in add() alongside
# NOISE_TOKENS as an extra guard on top of the message-capturing regex.
GENERIC_EXCEPTION_TOKENS = {t.lower() for t in {
    "Exception", "BaseException", "ValueError", "KeyError", "TypeError",
    "IndexError", "AttributeError", "RuntimeError", "AssertionError",
    "OSError", "IOError", "ImportError", "LookupError", "NameError",
    "NotImplementedError", "TimeoutError", "ConnectionError",
    "FileNotFoundError", "PermissionError", "StopIteration",
    "ZeroDivisionError", "OverflowError", "RecursionError", "SystemError",
}}


def strip_html(text: str) -> str:
    """Render Redmine HTML descriptions to plain text good enough for mining.

    Not a full HTML parser - we only care about visible text, and Redmine's
    output is structured well enough that <tag> stripping + entity decoding
    is sufficient. Preserves <pre> content (where stack traces/CLI live).
    """
    if not text:
        return ""
    # Replace <br>/<p>/<li>/<tr> with newlines so list items don't run together
    t = re.sub(r"</?(?:br|p|li|tr|h[1-6])\s*/?>", "\n", text, flags=re.I)
    # Strip remaining tags
    t = re.sub(r"<[^>]+>", "", t)
    # Decode HTML entities (ampersand, em-dash, non-breaking-space, etc.)
    t = html.unescape(t)
    # Collapse runs of blank lines
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# Redmine bug templates use a small vocabulary of section headers. Detect each
# one (<h1>..<h6> wrapper already stripped by strip_html, so they appear as
# isolated lines in the plain text). For each header, the section runs until
# the next header line.
SECTION_HEADERS = {
    "description": re.compile(r"^\s*Description\s*[:.]?\s*$", re.I),
    "steps_to_reproduce": re.compile(r"^\s*(?:Steps?\s+(?:to\s+)?Reproduce|Reproduction|Repro|How\s+to\s+Reproduce)\s*[:.]?\s*$", re.I),
    "expected_behavior": re.compile(r"^\s*(?:Expected\s+(?:Behavior|Result|Output)?|Expected)\s*[:.]?\s*$", re.I),
    "actual_behavior": re.compile(r"^\s*(?:Actual\s+(?:Behavior|Result|Output)?|Actual|Observed)\s*[:.]?\s*$", re.I),
    "evidence": re.compile(r"^\s*(?:Evidence|Logs?|Output|Console)\s*[:.]?\s*$", re.I),
    "impact": re.compile(r"^\s*(?:Impact|Severity|Risk)\s*[:.]?\s*$", re.I),
    "root_cause": re.compile(r"^\s*(?:Root\s+Cause(?:\s+Analysis)?|Cause|Why)\s*[:.]?\s*$", re.I),
    "environment": re.compile(r"^\s*(?:Affected\s+Version\s*/?\s*Environment|Environment|Affected\s+Version)\s*[:.]?\s*$", re.I),
    "recommendations": re.compile(r"^\s*(?:Recommendations?|Suggested\s+Fix|Workaround|Next\s+Steps)\s*[:.]?\s*$", re.I),
}

# Source-code references inside a description: pytest traceback frames, error output
# pasted from CLI, etc. These help a human (and the matcher) tie the bug back
# to specific files.
CODE_REF_RES = [
    # Python traceback: File "/path/to/foo.py", line 142
    re.compile(r'File\s+"([^"]+\.py)",\s+line\s+(\d+)'),
    # Generic file:line in CLI output
    re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_./-]*\.(?:py|sh|yaml|yml|json|c|cpp|h|go)):(\d+)\b'),
    # Pytest node ids: ngts/tests_nvos/foo/test_bar.py::test_baz
    re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_./-]*\.py)::([a-zA-Z_][a-zA-Z0-9_]*(?:\[[^\]]*\])?)\b'),
]


def parse_sections(plain_desc: str) -> dict:
    """Split a Redmine bug description into named sections.

    Returns a dict {section_name: text}. Section names are normalized
    (e.g. "Steps to Reproduce" -> "steps_to_reproduce"). Only non-empty
    sections appear in the output.
    """
    if not plain_desc:
        return {}
    lines = plain_desc.splitlines()
    sections: dict = {}
    current_key: str | None = None
    buf: list = []

    def flush():
        if current_key and buf:
            text = "\n".join(buf).strip()
            if text:
                # Preserve only the first time a header appears; later
                # occurrences (rare) are appended.
                existing = sections.get(current_key, "")
                sections[current_key] = (existing + "\n\n" + text).strip() if existing else text

    for raw in lines:
        stripped = raw.strip()
        matched_key = None
        for key, rx in SECTION_HEADERS.items():
            if rx.match(stripped):
                matched_key = key
                break
        if matched_key:
            flush()
            current_key = matched_key
            buf = []
        else:
            if current_key:
                buf.append(raw)
    flush()
    # Cap each section to keep the file size reasonable
    return {k: (v[:2000].rstrip() + "..." if len(v) > 2000 else v) for k, v in sections.items()}


def extract_code_refs(plain_desc: str) -> list[str]:
    if not plain_desc:
        return []
    refs: list[str] = []
    seen = set()
    for rx in CODE_REF_RES:
        for m in rx.finditer(plain_desc):
            if rx.groups == 2:
                ref = f"{m.group(1)}:{m.group(2)}"
            else:
                ref = m.group(0)
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs[:20]  # cap so a chatty stack trace doesn't dominate


def classify(subject: str) -> str:
    return "log_analyzer" if LOG_ANALYZER_PREFIX_RE.search(subject) else "feature"


def extract_log_analyzer_pattern(subject: str) -> str:
    sub = re.sub(r"^Copy of #\d+\s*-\s*", "", subject)
    sub = LOG_ANALYZER_PREFIX_RE.sub("", sub)
    return sub.strip()


# When a bug description has a speculative "Affected tests: test_a, test_b"
# section, those test names are author guesses, not confirmed pairings.
# Tests mentioned only inside such a section are dropped to avoid false
# positives like attributing an ACL upgrade failure to an ib-speed bug just
# because both tests were listed.
SPECULATIVE_HEADER_RE = re.compile(
    r"\b(?:Affected\s+tests?|Tests?\s+affected|Related\s+tests?|"
    r"Also\s+affects?|Likely\s+affects?)\s*[:\-]",
    re.IGNORECASE,
)


def _split_speculative(text: str) -> tuple[str, str]:
    """Return (primary, speculative) parts of `text`."""
    if not text:
        return "", ""
    m = SPECULATIVE_HEADER_RE.search(text)
    if not m:
        return text, ""
    return text[:m.start()], text[m.start():]


def extract_tests(text: str) -> list[str]:
    if not text:
        return []
    primary, speculative = _split_speculative(text)
    primary_hits = set(TEST_RE.findall(primary))
    speculative_hits = set(TEST_RE.findall(speculative))
    # Trust both lists. README convention is that bug filers add
    # "Affected tests: test_X, test_Y" to teach the matcher; treating that
    # list as speculative and dropping its entries silently broke the
    # documented workflow. Over-attribution risk for log_analyzer bugs is
    # handled separately by the matcher's log_analyzer + test-name-only
    # DROP rule (see bug_marker.py).
    confirmed = primary_hits | speculative_hits
    return [t for t in sorted(confirmed) if t not in NOISE_TESTS]


def extract_setup_filters(text: str) -> list[str]:
    if not text:
        return []
    hits = set()
    for rx, tag in SETUP_PATTERNS:
        if rx.search(text):
            hits.add(tag)
    return sorted(hits)


def extract_error_patterns(kind: str, subject: str, plain_desc: str) -> list[str]:
    patterns: list[str] = []

    def add(pat: str) -> None:
        pat = pat.strip()
        if not pat:
            return
        if pat in patterns:
            return
        # Filter generic / too-short tokens that would over-match
        low = pat.lower().strip("'\"")
        if low in NOISE_TOKENS or low in GENERIC_EXCEPTION_TOKENS:
            return
        if 3 <= len(pat) <= 240:
            patterns.append(pat)

    if kind == "log_analyzer":
        ep = extract_log_analyzer_pattern(subject)
        if ep:
            add(ep)

    if plain_desc:
        for rx in ASSERTION_RES:
            for m in rx.finditer(plain_desc):
                phrase = (m.group(1) if m.lastindex else m.group(0)).strip()
                if 6 <= len(phrase) <= 240:
                    add(phrase)
                # Also explode any quoted tokens inside the phrase as their
                # own patterns - failures quote individual fields, not the
                # whole Python list-repr we mined. Require >=6 chars so
                # short generic words ('ok', 'type', 'state') are skipped
                # even if they aren't already in NOISE_TOKENS.
                for tok_m in QUOTED_TOKEN_RE.finditer(phrase):
                    tok = tok_m.group(1)
                    if len(tok) >= 6:
                        add(tok)

    return patterns[:16]  # cap so a chatty description does not balloon the rule set


REJECTED_STATUS_NAMES = {"Rejected", "Won't Fix", "Won't fix", "Cannot Reproduce"}


def build_entry(ticket: dict, status_kind: str = "open") -> dict:
    """Build a baseline entry from a Redmine ticket.

    `status_kind` overrides the categorization for downstream consumers.
    Pass "rejected" for bugs fetched via fetch_rejected_tickets_live so the
    matcher / categories.json can route them to the 'test may need update'
    bucket instead of 'open bugs'.
    """
    subject = ticket.get("subject") or ""
    raw_desc = ticket.get("description") or ""
    plain_desc = strip_html(raw_desc)
    kind = classify(subject)
    # Auto-detect from status name if not explicitly passed
    status_name = (ticket.get("status") or {}).get("name", "")
    if status_kind == "open" and status_name in REJECTED_STATUS_NAMES:
        status_kind = "rejected"
    haystack = f"{subject}\n{plain_desc}"
    sections = parse_sections(plain_desc)
    # Cap full description body too, keep first 4k chars so the file stays
    # readable as text. The full original is always one HTTP call away
    # via the Redmine URL.
    description_short = plain_desc if len(plain_desc) <= 4000 else plain_desc[:4000].rstrip() + "..."
    return {
        "redmine_id": ticket["id"],
        "url": ticket.get("url") or f"https://redmine.mellanox.com/issues/{ticket['id']}",
        "subject": subject,
        "status": status_name,
        "status_kind": status_kind,  # "open" | "rejected"
        "priority": (ticket.get("priority") or {}).get("name", ""),
        "target_version": (ticket.get("fixed_version") or {}).get("name", ""),
        "kind": kind,
        "tests": extract_tests(f"{subject}\n{plain_desc}"),
        "error_patterns": extract_error_patterns(kind, subject, plain_desc),
        "setup_filters": extract_setup_filters(haystack),
        # Human-readable context: lets a meeting attendee skim the bug without
        # opening Redmine, and gives the matcher (or an LLM) richer signal for
        # semantic confidence checks.
        "description": description_short,
        "sections": sections,
        "code_refs": extract_code_refs(plain_desc),
        "sources": ["redmine_query_36102"],
    }


REJECTED_STATUS_IDS = [6]    # Redmine "Rejected" status. Extend if "Won't Fix"
# / "Cannot Reproduce" need surfacing too.
REJECTED_LOOKBACK_DAYS = 90  # Only include Rejected bugs updated in last N days.


def _fetch_paginated(list_url: str, params: dict, headers: dict,
                     hydrate_url_prefix: str, page_size: int) -> list[dict]:
    """Paginate /issues.json. Hydrates per-issue if description is absent."""
    import requests
    tickets: list = []
    offset = 0
    total = None
    while True:
        p = dict(params)
        p["limit"] = page_size
        p["offset"] = offset
        r = requests.get(list_url, headers=headers, params=p, timeout=60)
        r.raise_for_status()
        doc = r.json()
        page = doc.get("issues", [])
        total = doc.get("total_count", total)
        if not page:
            break
        if page and not page[0].get("description"):
            hydrated = []
            for t in page:
                detail = requests.get(
                    f"{hydrate_url_prefix}/{t['id']}.json",
                    headers=headers, timeout=30,
                ).json().get("issue", {})
                if detail:
                    hydrated.append(detail)
            page = hydrated
        tickets.extend(page)
        offset += len(page)
        if total is not None and offset >= total:
            break
        if len(page) < page_size:
            break
    return tickets


def fetch_rejected_tickets_live(
    project_id: int = PROJECT_ID,
    tracker_id: int = 28,  # Bug SW (matches the saved query's filter)
    lookback_days: int = REJECTED_LOOKBACK_DAYS,
    page_size: int = 100,
) -> list[dict]:
    """Fetch Rejected bugs from the last N days.

    Used to surface 'test may need update' attributions: when a failing test
    matches a rejected bug, dev has said 'not a bug, work as designed' - so
    the failure is a test/expectation issue, not a product issue.

    Same auth + pagination logic as fetch_tickets_live, but uses status_id=6
    instead of a saved query (saved queries can't filter to a specific
    inactive status without server-side changes).
    """
    import os
    import datetime as _dt
    token = os.getenv("REDMINE_API_TOKEN")
    if not token:
        raise RuntimeError("REDMINE_API_TOKEN env var is unset")
    headers = {"X-Redmine-API-Key": token, "Accept": "application/json"}

    list_url = f"{REDMINE_API_BASE}/projects/{project_id}/issues.json"
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=lookback_days)) \
        .strftime("%Y-%m-%d")
    params = {
        "tracker_id": tracker_id,
        "status_id": ",".join(str(s) for s in REJECTED_STATUS_IDS),
        "updated_on": f">={cutoff}",
        "include": "description",
    }
    return _fetch_paginated(list_url, params, headers,
                            f"{REDMINE_API_BASE}/issues", page_size)


def fetch_tickets_live(
    query_id: int = QUERY_ID,
    project_id: int = PROJECT_ID,
    page_size: int = 100,
) -> list[dict]:
    """Fetch all tickets from a Redmine saved query via REST, with description.

    Uses REDMINE_API_TOKEN env var (same convention as check_redmine_issues.py).
    Paginates until total_count is exhausted. Returns tickets in the same shape
    as the MCP cache, so build_entry() works unchanged.

    Saved queries are project-scoped, so the endpoint is
    /projects/<id>/issues.json (using /issues.json alone returns 404).
    Standard list endpoint doesn't include description by default; we pass
    include=description explicitly.

    Raises RuntimeError if REDMINE_API_TOKEN is unset, or any HTTP error.
    """
    token = os.getenv("REDMINE_API_TOKEN")
    if not token:
        raise RuntimeError(
            "REDMINE_API_TOKEN env var is unset. Run with "
            "`export REDMINE_API_TOKEN=<token>` (same token as "
            "check_redmine_issues.py uses) or pass --from-mcp-cache instead."
        )
    headers = {"X-Redmine-API-Key": token, "Accept": "application/json"}
    list_url = f"{REDMINE_API_BASE}/projects/{project_id}/issues.json"
    params = {
        "query_id": query_id,
        "include": "description",
        # Pull all matching statuses from the saved query (the query itself
        # filters; "*" prevents Redmine from re-applying the default
        # "open only" filter on top).
        "status_id": "*",
    }
    return _fetch_paginated(list_url, params, headers,
                            f"{REDMINE_API_BASE}/issues", page_size)


def load_tickets_from_mcp_cache(glob_pattern: str) -> list[dict]:
    """Read get_tickets-style tool-result files and return flat ticket list."""
    seen: dict[int, dict] = {}
    for path in sorted(glob.glob(glob_pattern)):
        try:
            with open(path) as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        # MCP get_tickets shape: {"data": {"tickets": [...]}}
        # MCP resolve_redmine_url shape: {"tickets": [...]}
        if isinstance(doc.get("data"), dict) and "tickets" in doc["data"]:
            tickets = doc["data"]["tickets"]
        elif "tickets" in doc:
            tickets = doc["tickets"]
        else:
            continue
        for t in tickets:
            tid = t.get("id")
            if tid is None:
                continue
            # Prefer the ticket with a description (get_tickets) over summary-only
            existing = seen.get(tid)
            if existing is None or (not existing.get("description") and t.get("description")):
                seen[tid] = t
    return list(seen.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument(
        "--from-mcp-cache",
        help=("Glob pattern of MCP tool-result JSON files (yai__get_tickets / "
              "yai__resolve_redmine_url) to read instead of calling Redmine. "
              "Useful inside a Claude session."),
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help=("Fetch tickets from Redmine REST in real time. Requires "
              "REDMINE_API_TOKEN env var. Default mode for cron / MARS."),
    )
    ap.add_argument(
        "--query-id",
        type=int,
        default=QUERY_ID,
        help=f"Redmine saved-query ID to pull from (default: {QUERY_ID}).",
    )
    args = ap.parse_args()

    if args.from_mcp_cache and args.live:
        print("ERROR: --from-mcp-cache and --live are mutually exclusive",
              file=sys.stderr)
        sys.exit(2)
    if not args.from_mcp_cache and not args.live:
        print("ERROR: pass --live (REDMINE_API_TOKEN required) or "
              "--from-mcp-cache GLOB", file=sys.stderr)
        sys.exit(2)

    if args.live:
        print(f"fetching open tickets from Redmine query {args.query_id} (live)...")
        open_tickets = fetch_tickets_live(query_id=args.query_id)
        print(f"  fetched {len(open_tickets)} open tickets")
        print(f"fetching rejected tickets from last {REJECTED_LOOKBACK_DAYS} days...")
        try:
            rejected_tickets = fetch_rejected_tickets_live()
            print(f"  fetched {len(rejected_tickets)} rejected tickets")
        except Exception as exc:
            print(f"  WARN: rejected-bugs fetch failed (non-fatal): {exc}", file=sys.stderr)
            rejected_tickets = []
    else:
        open_tickets = load_tickets_from_mcp_cache(args.from_mcp_cache)
        rejected_tickets = []
    if not open_tickets and not rejected_tickets:
        src = args.from_mcp_cache or f"Redmine query {args.query_id}"
        print(f"ERROR: no tickets found from {src}", file=sys.stderr)
        sys.exit(1)

    bugs = [build_entry(t, status_kind="open") for t in open_tickets]
    bugs.extend(build_entry(t, status_kind="rejected") for t in rejected_tickets)
    # Dedupe: if a bug appears in both batches (rare race), prefer rejected
    # (more conservative attribution) by sorting open first then dropping dupes.
    seen_ids: set = set()
    deduped: list = []
    for b in sorted(bugs, key=lambda b: b["status_kind"] == "rejected"):  # open first
        rid = b["redmine_id"]
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        deduped.append(b)
    bugs = sorted(deduped, key=lambda b: b["redmine_id"], reverse=True)

    out = {
        "_meta": {
            "source_query_url": QUERY_URL,
            "source_query_name": QUERY_NAME,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ticket_count": len(bugs),
            "schema_version": 3,
        },
        "bugs": bugs,
    }

    # Write atomically: temp file in the same dir + rename. Readers never see
    # a half-written baseline (matters because the matcher polls this file
    # at session start across many pytest runs).
    out_dir = os.path.dirname(args.out) or "."
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = args.out + ".tmp"
    with open(tmp_path, "w") as fh:
        json.dump(out, fh, indent=2)
    os.replace(tmp_path, args.out)

    # Stats
    n_open = sum(1 for b in bugs if b["status_kind"] == "open")
    n_rejected = sum(1 for b in bugs if b["status_kind"] == "rejected")
    n_with_tests = sum(1 for b in bugs if b["tests"])
    n_with_err = sum(1 for b in bugs if b["error_patterns"])
    n_with_setup = sum(1 for b in bugs if b["setup_filters"])
    n_log = sum(1 for b in bugs if b["kind"] == "log_analyzer")
    setup_freq: dict[str, int] = {}
    for b in bugs:
        for s in b["setup_filters"]:
            setup_freq[s] = setup_freq.get(s, 0) + 1

    print(f"wrote {args.out}")
    print(f"  total bugs           : {len(bugs)}  ({n_open} open, {n_rejected} rejected)")
    print(f"  with tests[]         : {n_with_tests}")
    print(f"  with error_patterns[]: {n_with_err}")
    print(f"  with setup_filters[] : {n_with_setup}")
    print(f"  log_analyzer kind    : {n_log}")
    print(f"  feature kind         : {len(bugs) - n_log}")
    print(f"  setup distribution   : {setup_freq}")


if __name__ == "__main__":
    main()
