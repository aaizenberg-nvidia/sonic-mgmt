"""
Surface @pytest.mark.bug(redmine=...) as Allure issue links + filterable tags.

Test authors annotate a test with the open ticket(s) tracking its current failure
once, in the test file. The conftest hook (pytest_collection_modifyitems) calls
``apply_bug_marker_to_item`` for every collected item, which converts each
``@pytest.mark.bug`` marker into native allure-pytest markers:

    * ``allure_link``  -> clickable Redmine bug-icon on the test detail page
    * ``allure_label`` (``tag=known_bug``)        -> chip + tag filter on the
      test list, same UI control as the existing ``flaky`` chip
    * ``allure_label`` (``tag=known_bug_<id>``)   -> per-ticket chip + filter so
      the meeting can pivot on a single Redmine number

Usage in tests::

    @pytest.mark.bug(redmine=4839922, note="UFM-MAD loses SLAAC after DHCP disable")
    def test_ipv6_slaac_after_dhcp_disable():
        ...

    @pytest.mark.bug(redmine=[4874912, 4839922])
    def test_fatal_mode_soft_reset_fan_recovery():
        ...

    @pytest.mark.bug(redmine=4839922, nvbugs=5721616)
    def test_with_both_trackers():
        ...
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from typing import Iterable, List, Optional

import pytest
from allure_commons.types import LabelType, LinkType

REDMINE_ISSUE_URL = "https://redmine.mellanox.com/issues/{id}"
NVBUGS_ISSUE_URL = "https://nvbugs/{id}"

KNOWN_BUG_TAG = "known_bug"
KNOWN_BUG_TAG_PREFIX_REDMINE = "known_bug_redmine_"
KNOWN_BUG_TAG_PREFIX_NVBUGS = "known_bug_nvbugs_"
# A separate top-level tag for tests matching a rejected (not-a-bug) bug so
# the meeting can filter them as "test may need update" instead of mixing
# them with active product bugs.
REJECTED_BUG_TAG = "rejected_bug"

# Baseline of currently-open bugs. Regenerated nightly into the shared
# verification dir by ngts/scripts/allure_summary/sync_known_bugs.py (via the
# MARS "Sync known-bugs baseline" step or a cron on a player). The conftest
# hook reads it at session start so every test run, on every player, sees
# the same up-to-date Redmine truth.
#
# The shared path is preferred; if the player can't see /auto/sw_system_project
# (e.g. dev workstation without the mount), we fall back to an in-tree copy.
BASELINE_PATH_SHARED = "/auto/sw_system_project/NVOS_INFRA/bug_attribution/known_bugs_baseline.json"
BASELINE_PATH_IN_TREE = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "..", "scripts", "allure_summary", "known_bugs_baseline.json",
    )
)

# Hand-curated test->bug mapping. Authoritative for attribution at runtime.
# When this file exists, the matcher prefers it over the auto-extracted
# baseline (which is kept as a secondary signal / future training data).
MAPPINGS_PATH_SHARED = "/auto/sw_system_project/NVOS_INFRA/bug_attribution/known_bugs_mappings.json"

# Aliases used to match a mapping's `system_type` against the report's
# setup name. Centralized so each new switch family adds one line.
SYSTEM_TYPE_ALIASES = {
    # Canonical keys match the sync_known_bugs.py SETUP_PATTERNS output
    # vocabulary (croc, bm, taipan). Aliases let _setup_matches in
    # ai_attribute_report.py and _bug_setup_match here recognize MARS
    # setup-name variants - mtvr-q3400-XX setups are bm machines, so
    # q3400 / qm3400 / quantum-3 are listed as bm aliases.
    "bm": ["bm", "mamba", "black-mamba", "blackmamba", "mamba-dgx",
           "white-mamba", "q3400", "qm3400", "quantum-3", "quantum3"],
    "croc": ["croc", "crocodile"],
    "taipan": ["taipan"],
    # Retained for historical project names / operator notes - extractor
    # no longer emits these as setup_filters, but the agent's project-name
    # heuristics in derive_setup_from_project still recognize them.
    "juliet": ["juliet"],
    "rosalind": ["rosalind"],
    "gorilla": ["gorilla"],
}


def _default_baseline_path() -> str:
    if os.path.exists(BASELINE_PATH_SHARED):
        return BASELINE_PATH_SHARED
    return BASELINE_PATH_IN_TREE


BASELINE_PATH = _default_baseline_path()

_PARAM_RE = re.compile(r"\[.*\]$")

# Sentinels appended to failed-test messages so the Allure Categories widget
# can bucket them. Keep these SHORT - they appear on every failed-test row in
# the Categories tree, so any verbose subject/note clutters the leaf summary.
# Full bug context lives in the test's clickable issue link (test detail page).
KNOWN_BUG_SENTINEL_PREFIX = "[Known bug: "
KNOWN_BUG_SENTINEL_SUFFIX = "]"
# Failures matching a Rejected bug get this sentinel instead - dev has said
# 'not a bug, work as designed', so the failure points at a test that likely
# needs updating, not a product fix.
REJECTED_BUG_SENTINEL_PREFIX = "[Rejected bug (test may need update): "
REJECTED_BUG_SENTINEL_SUFFIX = "]"
NO_KNOWN_BUG_SENTINEL = "[No known bug]"

# Regex matching any prior sentinel at end-of-message (with or without leading
# blank line). Used to strip stale sentinels before appending a fresh one - the
# AI agent and the offline matcher both inherit statusMessage from prior runs,
# and an earlier (now-stale) attribution must not stack on top of the new one.
# Covers legacy "[Known open bug: ...]" / "[No known open bug]" variants too.
_SENTINEL_STRIP_RE = re.compile(
    r"\s*\["
    r"(?:Known(?:\s+open)?\s+bug"
    r"|Rejected\s+bug\s*\(test\s+may\s+need\s+update\)"
    r"|No\s+known(?:\s+open)?\s+bug)"
    r"(?::[^\]]*)?\]\s*$",
    re.IGNORECASE,
)


def strip_prior_sentinels(msg: str) -> str:
    """Strip any trailing known-bug / rejected-bug / no-known-bug sentinel(s).
    Idempotent and stack-safe - applies repeatedly to peel off multiple
    sentinels that may have accumulated across re-render passes.
    """
    if not msg:
        return msg
    prev = None
    while prev != msg:
        prev = msg
        msg = _SENTINEL_STRIP_RE.sub("", msg).rstrip()
    return msg


# Categories.json rules that populate the Categories dashboard widget.
# Two mutually-exclusive buckets so the meeting can pivot between
# "what's already tracked" and "what's actually new".
KNOWN_BUG_CATEGORIES = [
    {
        "name": "Failures caused by open bugs",
        "matchedStatuses": ["failed", "broken"],
        # Match both the new short form and the legacy verbose form so older
        # reports re-rendered with the previous sentinel still bucket correctly.
        "messageRegex": r"(?s).*\[Known (?:open )?bug:[^\]]+\].*",
    },
    {
        "name": "Failures matching a rejected bug (test may need update)",
        "matchedStatuses": ["failed", "broken"],
        "messageRegex": r"(?s).*\[Rejected bug \(test may need update\):[^\]]+\].*",
    },
    {
        "name": "Failures without a known bug",
        "matchedStatuses": ["failed", "broken"],
        "messageRegex": r"(?s).*\[No known (?:open )?bug\].*",
    },
]

_log = logging.getLogger(__name__)


def _coerce_id_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _link_name(prefix: str, ticket_id: str, note: str) -> str:
    base = f"{prefix} #{ticket_id}"
    return f"{base} - {note}" if note else base


def apply_bug_marker_to_item(item) -> None:
    """For every @pytest.mark.bug on `item`, attach the equivalent allure markers.

    Idempotent for the ``known_bug`` tag (allure-pytest merges duplicate labels
    by (label_type, value)). Per-ticket tags are unique by ID.
    """
    has_bug = False

    for mark in item.iter_markers(name="bug"):
        redmine_ids = _coerce_id_list(mark.kwargs.get("redmine"))
        nvbugs_ids = _coerce_id_list(mark.kwargs.get("nvbugs"))
        note = (mark.kwargs.get("note") or "").strip()

        for rid in redmine_ids:
            url = REDMINE_ISSUE_URL.format(id=rid)
            item.add_marker(
                pytest.mark.allure_link(
                    url, link_type=LinkType.ISSUE, name=_link_name("Redmine", rid, note)
                )
            )
            item.add_marker(
                pytest.mark.allure_label(
                    f"{KNOWN_BUG_TAG_PREFIX_REDMINE}{rid}", label_type=LabelType.TAG
                )
            )
            has_bug = True

        for nid in nvbugs_ids:
            url = NVBUGS_ISSUE_URL.format(id=nid)
            item.add_marker(
                pytest.mark.allure_link(
                    url, link_type=LinkType.ISSUE, name=_link_name("NVbugs", nid, note)
                )
            )
            item.add_marker(
                pytest.mark.allure_label(
                    f"{KNOWN_BUG_TAG_PREFIX_NVBUGS}{nid}", label_type=LabelType.TAG
                )
            )
            has_bug = True

    if has_bug:
        item.add_marker(
            pytest.mark.allure_label(KNOWN_BUG_TAG, label_type=LabelType.TAG)
        )


def apply_bug_markers(items: Iterable) -> None:
    for item in items:
        apply_bug_marker_to_item(item)


def load_mappings(path: str = MAPPINGS_PATH_SHARED) -> Optional[dict]:
    """Load the hand-curated test->bug mapping file (or None if missing)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("bug_marker: mappings at %s unreadable: %s", path, exc)
        return None


def _system_type_matches(system_types, setup_name: str) -> bool:
    """A mapping's system_type matches the report's setup if any alias of
    any listed type appears as a substring of setup_name (case-insensitive).
    Empty / unset system_type means 'matches anywhere'.
    """
    if not system_types:
        return True
    if not setup_name:
        return True
    if isinstance(system_types, str):
        system_types = [system_types]
    name_lc = setup_name.lower()
    for st in system_types:
        st_lc = (st or "").strip().lower()
        if not st_lc:
            continue
        # Direct substring match (e.g. "taipan" in "taipan-e2e-temp")
        if st_lc in name_lc:
            return True
        # Or any alias of this canonical platform
        for alias in SYSTEM_TYPE_ALIASES.get(st_lc, []):
            if alias.lower() in name_lc:
                return True
    return False


def _mapping_bug_link(mapping: dict) -> dict:
    """Build the allure issue-link dict for a curated mapping entry.

    Prefer redmine_id; fall back to nvbugs_id.
    """
    rid = mapping.get("redmine_id")
    nid = mapping.get("nvbugs_id")
    subject = (mapping.get("subject") or "").strip()
    if rid:
        url = REDMINE_ISSUE_URL.format(id=rid)
        name = f"Redmine #{rid}" + (f" - {subject[:90]}" if subject else "")
    elif nid:
        url = f"https://nvbugspro.nvidia.com/bug/{nid}"
        name = f"NVbugs #{nid}" + (f" - {subject[:90]}" if subject else "")
    else:
        return {}
    return {"type": "issue", "url": url, "name": name}


def _mapping_tags(mapping: dict) -> List[str]:
    out = [KNOWN_BUG_TAG]
    rid = mapping.get("redmine_id")
    nid = mapping.get("nvbugs_id")
    if rid:
        out.append(f"{KNOWN_BUG_TAG_PREFIX_REDMINE}{rid}")
    if nid:
        out.append(f"{KNOWN_BUG_TAG_PREFIX_NVBUGS}{nid}")
    return out


def find_mapping_for_failure(
    test_name: str,
    error_message: str,
    setup_name: str,
    mappings_doc: Optional[dict] = None,
) -> List[dict]:
    """Return curated mappings that bind to this failure under the strict AND:
       system_type matches setup AND test_name matches exactly (with `[...]`
       suffix) AND error_msg is a case-insensitive substring of error_message.
    """
    if mappings_doc is None:
        mappings_doc = load_mappings()
    if not mappings_doc:
        return []
    haystack = (error_message or "").lower()
    out: List[dict] = []
    for m in mappings_doc.get("mappings", []) or []:
        if m.get("test_name") != test_name:
            continue
        needle = (m.get("error_msg") or "").strip().lower()
        if needle and needle not in haystack:
            continue
        if not _system_type_matches(m.get("system_type"), setup_name):
            continue
        out.append(m)
    return out


def load_baseline(path: str = BASELINE_PATH) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("bug_marker: baseline at %s unreadable: %s", path, exc)
        return None


def _existing_link_urls(item) -> set:
    return {
        m.args[0] for m in item.iter_markers(name="allure_link")
        if m.args
    }


def _bug_link_name(bug: dict) -> str:
    subject = (bug.get("subject") or "").strip()
    rid = bug.get("redmine_id")
    if subject and len(subject) > 90:
        subject = subject[:87].rstrip() + "..."
    return f"Redmine #{rid}" + (f" - {subject}" if subject else "")


def _apply_baseline_bug_to_item(item, bug: dict) -> bool:
    url = bug.get("url") or REDMINE_ISSUE_URL.format(id=bug.get("redmine_id"))
    if url in _existing_link_urls(item):
        return False
    item.add_marker(
        pytest.mark.allure_link(
            url, link_type=LinkType.ISSUE, name=_bug_link_name(bug)
        )
    )
    item.add_marker(
        pytest.mark.allure_label(
            f"{KNOWN_BUG_TAG_PREFIX_REDMINE}{bug.get('redmine_id')}",
            label_type=LabelType.TAG,
        )
    )
    return True


def apply_baseline_to_items(items: Iterable, baseline: Optional[dict] = None) -> int:
    """Match each item against baseline.tests and attach allure markers.

    Only the test-name-based match runs at collection time; error-pattern
    matching for log_analyzer-kind bugs happens at session finish, when
    failure messages are available (see attach_baseline_to_results).

    Returns count of (item, bug) pairs applied.
    """
    if baseline is None:
        baseline = load_baseline()
    if not baseline:
        return 0
    applied = 0
    by_name: dict = {}
    for bug in baseline.get("bugs", []) or []:
        for t in bug.get("tests", []) or []:
            by_name.setdefault(t, []).append(bug)
    for item in items:
        base = _PARAM_RE.sub("", item.name)
        candidates = by_name.get(base, []) + [
            b for b in by_name.get(item.name, []) if b not in by_name.get(base, [])
        ]
        item_applied = False
        for matched in candidates:
            if _apply_baseline_bug_to_item(item, matched):
                applied += 1
                item_applied = True
        if not item_applied:
            continue
        already_tagged = any(
            (m.args and m.args[0] == KNOWN_BUG_TAG)
            for m in item.iter_markers(name="allure_label")
        )
        if not already_tagged:
            item.add_marker(
                pytest.mark.allure_label(KNOWN_BUG_TAG, label_type=LabelType.TAG)
            )
    return applied


def _issue_link_names(result):
    return [
        link.get("name") or link.get("url", "")
        for link in result.get("links", []) or []
        if link.get("type") == "issue" and (link.get("name") or link.get("url"))
    ]


def _append_sentinel(result: dict, sentinel: str) -> bool:
    details = result.setdefault("statusDetails", {})
    msg = strip_prior_sentinels((details.get("message") or "").rstrip())
    new_msg = (msg + ("\n\n" if msg else "") + sentinel).strip()
    if new_msg == (details.get("message") or ""):
        return False
    details["message"] = new_msg
    return True


def stamp_known_bug_sentinels(alluredir: str) -> dict:
    """Stamp every failed/broken test with one of two sentinels.

    Tests with an Allure issue link (i.e. `@pytest.mark.bug` was applied) get
    `[Known open bug: ...]`. Tests without get `[No known open bug]`. The
    sentinels are what Allure's categories.json regex matches against to
    populate the dashboard buckets.

    Returns counts: {"known": N, "unknown": M, "skipped": K}.
    """
    counts = {"known": 0, "unknown": 0, "skipped": 0}
    if not alluredir or not os.path.isdir(alluredir):
        return counts
    for path in glob.glob(os.path.join(alluredir, "*-result.json")):
        try:
            with open(path) as fh:
                result = json.load(fh)
        except (OSError, json.JSONDecodeError):
            counts["skipped"] += 1
            continue
        if result.get("status") not in ("failed", "broken"):
            continue
        names = _issue_link_names(result)
        # Detect rejected attribution by looking at the tag labels; if the
        # result carries a rejected_bug tag, use the rejected sentinel so
        # the failure buckets into "test may need update" instead of "open bugs".
        is_rejected = any(
            l.get("name") == "tag" and l.get("value") == REJECTED_BUG_TAG
            for l in (result.get("labels") or [])
        )
        if names:
            ids_only = [n.split(" - ")[0] for n in names]  # short form
            joined = ", ".join(ids_only)
            if is_rejected:
                sentinel = (
                    f"{REJECTED_BUG_SENTINEL_PREFIX}{joined}"
                    f"{REJECTED_BUG_SENTINEL_SUFFIX}"
                )
            else:
                sentinel = (
                    f"{KNOWN_BUG_SENTINEL_PREFIX}{joined}"
                    f"{KNOWN_BUG_SENTINEL_SUFFIX}"
                )
            bucket = "known"
        else:
            sentinel = NO_KNOWN_BUG_SENTINEL
            bucket = "unknown"
        if not _append_sentinel(result, sentinel):
            continue
        try:
            with open(path, "w") as fh:
                json.dump(result, fh)
            counts[bucket] += 1
        except OSError:
            counts["skipped"] += 1
    return counts


def write_categories_json(alluredir: str) -> bool:
    """Ensure categories.json carries the Known-open-bug rules.

    Merges by name so a coexisting categories.json from another plugin keeps
    its own rules. Returns True if the file was written/updated.
    """
    if not alluredir or not os.path.isdir(alluredir):
        return False
    path = os.path.join(alluredir, "categories.json")
    rules: List[dict] = []
    if os.path.exists(path):
        try:
            with open(path) as fh:
                loaded = json.load(fh)
            if isinstance(loaded, list):
                rules = loaded
        except (OSError, json.JSONDecodeError):
            rules = []
    by_name = {r.get("name"): i for i, r in enumerate(rules) if isinstance(r, dict)}
    for category in KNOWN_BUG_CATEGORIES:
        if category["name"] in by_name:
            rules[by_name[category["name"]]] = category
        else:
            rules.append(category)
    try:
        with open(path, "w") as fh:
            json.dump(rules, fh, indent=2)
        return True
    except OSError:
        return False


def _attach_bug_to_result(result: dict, bug: dict, existing_urls: set,
                          existing_labels: set) -> bool:
    url = bug.get("url") or REDMINE_ISSUE_URL.format(id=bug["redmine_id"])
    if url in existing_urls:
        return False
    is_rejected = bug.get("status_kind") == "rejected"
    name = _bug_link_name(bug)
    if is_rejected and "REJECTED" not in name:
        name = name + " [REJECTED - test may need update]"
    result.setdefault("links", []).append({
        "type": "issue",
        "url": url,
        "name": name,
    })
    top_tag = REJECTED_BUG_TAG if is_rejected else KNOWN_BUG_TAG
    tag_value = f"{KNOWN_BUG_TAG_PREFIX_REDMINE}{bug['redmine_id']}"
    for value in (top_tag, tag_value):
        if ("tag", value) not in existing_labels:
            result.setdefault("labels", []).append(
                {"name": "tag", "value": value}
            )
            existing_labels.add(("tag", value))
    existing_urls.add(url)
    return True


def _bug_setup_match(bug: dict, setup_name: Optional[str]) -> bool:
    """A bug applies to this setup if it has no setup_filters OR any filter
    (or one of its SYSTEM_TYPE_ALIASES) appears in the setup name.

    Alias lookup covers the case where the bug's setup_filter is the
    canonical platform name (e.g. "bm") but the MARS setup name uses the
    ASIC/hwsku token instead ("mtvr-q3400-06"). Without the alias map,
    simple substring matching here would miss those.
    """
    filters = bug.get("setup_filters") or []
    if not filters:
        return True
    if not setup_name:
        return True
    name_lc = setup_name.lower()
    for f in filters:
        f_lc = (f or "").strip().lower()
        if not f_lc:
            continue
        if f_lc in name_lc:
            return True
        for alias in SYSTEM_TYPE_ALIASES.get(f_lc, []):
            if alias.lower() in name_lc:
                return True
    return False


def _bug_error_patterns(bug: dict) -> list:
    """Return the bug's error_patterns[] or fallback to the legacy single
    error_pattern field. Empty list if neither is set."""
    patterns = bug.get("error_patterns")
    if patterns:
        return list(patterns)
    legacy = bug.get("error_pattern")
    return [legacy] if legacy else []


def score_failure_against_baseline(
    test_name: str,
    status_message: str,
    status_trace: str,
    baseline: dict,
    setup_name: Optional[str] = None,
    min_score: int = 60,
) -> Optional[tuple]:
    """Score a single failure against every bug in the baseline using the
    same deterministic rubric as `attach_baseline_to_failed_results`, and
    return the single highest-scoring (bug, score) if score >= min_score.

    Used by both the post-session offline matcher (which mutates result.json
    files) and the AI agent's pre-LLM gate (which works on test-case dicts
    fetched from the Allure REST API). Keeping one scoring function means
    the rubric in SCORING.md applies identically to both.

    Score legend (must match the inline scorer in
    `attach_baseline_to_failed_results`):
       100 = test_name exact match in bug.tests[] for non-log_analyzer kind
        80 = test_name prefix-with-underscore match (non-log_analyzer kind)
        60 = error_pattern hit in statusMessage
        20 = error_pattern hit in statusTrace ONLY (dropped for log_analyzer
             bugs to suppress teardown syslog noise)
         +5 feature-bug tiebreaker
        DROP = test-name match for log_analyzer-kind bug WITHOUT
               error_pattern hit in statusMessage. log_analyzer auto-fillings
               list affected tests speculatively; the syslog pattern must
               corroborate before attribution.
    """
    if not baseline:
        return None

    base_name = _PARAM_RE.sub("", test_name) if test_name else ""
    candidate_scores: dict = {}

    # Build indexes (cheap; baseline has ~1100 bugs)
    bugs_by_test: dict = {}
    tests_index_sorted: list = []
    bugs_with_patterns: list = []
    for bug in baseline.get("bugs", []) or []:
        for t in bug.get("tests", []) or []:
            bugs_by_test.setdefault(t, []).append(bug)
            tests_index_sorted.append((t, bug))
        compiled = []
        for raw in _bug_error_patterns(bug):
            try:
                compiled.append(re.compile(re.escape(raw)))
            except re.error:
                continue
        if compiled:
            bugs_with_patterns.append((bug, compiled))
    tests_index_sorted.sort(key=lambda kv: -len(kv[0]))

    # Pre-compute which bug IDs had an error_pattern hit in statusMessage,
    # so consider() can drop log_analyzer test-name matches that lack any
    # corroborating syslog evidence in the assertion text.
    msg = status_message or ""
    trace = status_trace or ""
    bugs_hit_in_msg: set = set()
    for bug, patterns in bugs_with_patterns:
        if msg and any(p.search(msg) for p in patterns):
            rid = bug.get("redmine_id") or bug.get("id")
            if rid is not None:
                bugs_hit_in_msg.add(rid)

    def consider(bug: dict, score: int, source: str = "") -> None:
        rid = bug.get("redmine_id") or bug.get("id")
        if not rid:
            return
        if not _bug_setup_match(bug, setup_name):
            return
        # A bug is only linked when at least one of its error_patterns
        # matches the failure's statusMessage. Test-name match alone is
        # not sufficient. Bugs with no error_patterns are never linked.
        if rid not in bugs_hit_in_msg:
            return
        if bug.get("kind") == "feature":
            score += 5
        prev = candidate_scores.get(rid)
        if prev is None or score > prev[0]:
            candidate_scores[rid] = (score, bug)

    # Test-name tiers.
    # Exact 100 stays for non-log_analyzer kinds: that's an explicit
    # "Affected tests" line written by the bug filer. Prefix-80 is INFERRED
    # expansion ("test_foo" -> "test_foo_extra"), which is dangerous for
    # log_analyzer bugs whose tests[] field collects every test where the
    # ERR appeared during teardown - those are siblings, not children.
    # Drop prefix-80 for log_analyzer (symmetric to the existing
    # trace-only-drop rule). For log_analyzer-kind bugs, even the exact
    # 100-tier requires corroborating error_pattern hit in statusMessage
    # (enforced inside consider()).
    for n in (base_name, test_name):
        for bug in bugs_by_test.get(n, []):
            consider(bug, 100, source="test_name")
    for stem, bug in tests_index_sorted:
        if base_name == stem or base_name == test_name:
            continue
        if base_name.startswith(stem + "_"):
            if bug.get("kind") == "log_analyzer":
                continue
            consider(bug, 80, source="test_name")

    # Error-pattern tiers
    for bug, patterns in bugs_with_patterns:
        rid = bug.get("redmine_id") or bug.get("id")
        in_msg = rid in bugs_hit_in_msg
        in_trace = (any(p.search(trace) for p in patterns)
                    if trace and not in_msg else False)
        if in_msg:
            consider(bug, 60)
        elif in_trace:
            if bug.get("kind") == "log_analyzer":
                continue
            consider(bug, 20)

    if not candidate_scores:
        return None
    score, best_bug = max(candidate_scores.values(), key=lambda sb: sb[0])
    if score < min_score:
        return None
    return (best_bug, score)


def attach_baseline_to_failed_results(
    alluredir: str,
    baseline: Optional[dict] = None,
    setup_name: Optional[str] = None,
) -> int:
    """For each failed/broken result, apply baseline matches by test-name AND
    by error-pattern, gated on setup_filters.

    This is the post-processing equivalent of the conftest collection hook +
    session-finish hook combined - so it works retroactively on existing
    Allure result directories that were produced before the plugin was wired.

    Args:
        alluredir   : directory of *-result.json files to mutate.
        baseline    : optional pre-loaded baseline dict.
        setup_name  : optional setup name (e.g. "croc-94"); enables
                      setup_filters filtering. If unset, all bugs are
                      considered eligible.

    Returns the number of (result, bug) attachments made.
    """
    if baseline is None:
        baseline = load_baseline()
    if not baseline:
        return 0

    mappings_doc = load_mappings()
    # Index open baseline bugs by redmine_id for fast status lookup when
    # applying curated mappings. Only open bugs should trigger attribution.
    baseline_open_by_id: dict = {
        b["redmine_id"]: b
        for b in (baseline.get("bugs") or [])
        if b.get("status_kind") == "open" and b.get("redmine_id")
    }

    bugs_by_test: dict = {}
    # tests_index_sorted lets us do prefix-with-underscore match: a baseline
    # entry "test_system_issu" should match the failing test
    # "test_system_issu_positive_basic_flow" but NOT "test_system_issuance".
    tests_index_sorted: list = []
    bugs_with_patterns: list = []
    for bug in baseline.get("bugs", []) or []:
        for t in bug.get("tests", []) or []:
            bugs_by_test.setdefault(t, []).append(bug)
            tests_index_sorted.append((t, bug))
        compiled = []
        for raw in _bug_error_patterns(bug):
            try:
                compiled.append(re.compile(re.escape(raw)))
            except re.error:
                continue
        if compiled:
            bugs_with_patterns.append((bug, compiled))
    # Sort by length DESC so the longest (most specific) stem matches first
    tests_index_sorted.sort(key=lambda kv: -len(kv[0]))

    if not bugs_by_test and not bugs_with_patterns:
        return 0

    attached = 0
    for path in glob.glob(os.path.join(alluredir, "*-result.json")):
        try:
            with open(path) as fh:
                result = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if result.get("status") not in ("failed", "broken"):
            continue
        existing_urls = {
            link.get("url") for link in result.get("links", []) or []
            if link.get("url")
        }
        existing_labels = {
            (lbl.get("name"), lbl.get("value"))
            for lbl in result.get("labels", []) or []
        }

        # Curated mappings take priority over baseline scoring. If the test has
        # an explicit entry in known_bugs_mappings.json, attach it and skip the
        # baseline candidate search entirely for this result.
        details_pre = result.get("statusDetails") or {}
        msg_pre = details_pre.get("message") or ""
        curated = find_mapping_for_failure(
            result.get("name", ""), msg_pre, setup_name or "",
            mappings_doc=mappings_doc,
        )
        if curated:
            rid = curated[0].get("redmine_id")
            if rid and rid not in baseline_open_by_id:
                # Bug is closed/resolved — fall through to baseline scoring
                # so the test gets [No known bug] rather than a stale link.
                curated = []
        if curated:
            bug_link = _mapping_bug_link(curated[0])
            if bug_link and bug_link.get("url") not in existing_urls:
                result.setdefault("links", []).append(bug_link)
                for tag in _mapping_tags(curated[0]):
                    if ("tag", tag) not in existing_labels:
                        result.setdefault("labels", []).append(
                            {"name": "tag", "value": tag}
                        )
                        existing_labels.add(("tag", tag))
                attached += 1
                try:
                    with open(path, "w") as fh:
                        json.dump(result, fh)
                except OSError:
                    pass
            continue

        # Collect all plausible matches with a score, then attach ONLY the
        # highest-scoring one. Multi-bug attribution per failure produced too
        # much noise (especially log_analyzer bugs whose error_pattern fires
        # on syslog lines the loganalyzer plugin pulls into the trace during
        # teardown of unrelated tests).
        # Score legend (higher = stronger evidence):
        #    60 = error_pattern hit in statusMessage (required to fire at all)
        #   +100 = also has test-name exact match in bug.tests[] (non log_analyzer)
        #    +80 = also has test-name prefix-with-underscore match (non log_analyzer)
        #    +20 = error_pattern hit in statusTrace only (weak, non log_analyzer)
        #     +5 = feature-kind tiebreaker over log_analyzer-kind
        # A bug that has no error_patterns, or whose patterns don't match
        # statusMessage, is never linked regardless of test-name match.
        candidate_scores: dict = {}  # redmine_id -> (score, bug)

        # Pre-compute which bugs had an error_pattern hit in statusMessage,
        # so test-name scoring can demote log_analyzer bugs that lack it.
        details = result.get("statusDetails") or {}
        msg = details.get("message") or ""
        bugs_hit_in_msg: set = set()
        for bug, patterns in bugs_with_patterns:
            if msg and any(p.search(msg) for p in patterns):
                bugs_hit_in_msg.add(bug.get("redmine_id"))

        def consider(bug: dict, score: int, source: str = "") -> None:
            if not _bug_setup_match(bug, setup_name):
                return
            rid = bug.get("redmine_id")
            if rid is None:
                return
            # A bug is only linked when at least one of its error_patterns
            # matches the failure's statusMessage. Test-name match alone is
            # not sufficient — the same test can fail for many unrelated
            # reasons, and test-name-only attribution produces too many false
            # positives. Bugs with no error_patterns defined are never linked.
            if rid not in bugs_hit_in_msg:
                return
            if bug.get("kind") == "feature":
                score += 5
            prev = candidate_scores.get(rid)
            if prev is None or score > prev[0]:
                candidate_scores[rid] = (score, bug)

        # Test-name matches
        name = result.get("name", "")
        base_name = _PARAM_RE.sub("", name)
        for n in (base_name, name):
            for bug in bugs_by_test.get(n, []):
                consider(bug, 100, source="test_name")
        for stem, bug in tests_index_sorted:
            if base_name == stem or base_name == name:
                continue
            if base_name.startswith(stem + "_"):
                # log_analyzer bugs collect siblings via teardown-syslog
                # scans; their tests[] entries are not parent stems. Drop
                # prefix-80 to avoid attributing unrelated tests in the
                # same family.
                if bug.get("kind") == "log_analyzer":
                    continue
                consider(bug, 80, source="test_name")

        # Error-pattern matches. Score depends on WHERE the pattern matched:
        # statusMessage = the assertion text (strong signal).
        # statusTrace only = often loganalyzer-teardown noise (weak signal),
        # and for log_analyzer-kind bugs we DROP trace-only matches entirely
        # because their syslog patterns fire in many unrelated tests' traces.
        trace = details.get("trace") or ""
        for bug, patterns in bugs_with_patterns:
            in_msg = bug.get("redmine_id") in bugs_hit_in_msg
            in_trace = (any(p.search(trace) for p in patterns)
                        if trace and not in_msg else False)
            if in_msg:
                consider(bug, 60)
            elif in_trace:
                if bug.get("kind") == "log_analyzer":
                    # Drop noisy log_analyzer trace-only matches - they fire
                    # on syslog lines unrelated tests pull into their trace.
                    continue
                consider(bug, 20)

        # Pick the single highest-scoring candidate
        if candidate_scores:
            _score, best_bug = max(candidate_scores.values(),
                                   key=lambda sb: sb[0])
            if _attach_bug_to_result(result, best_bug, existing_urls, existing_labels):
                attached += 1
                try:
                    with open(path, "w") as fh:
                        json.dump(result, fh)
                except OSError:
                    pass
    return attached


def finalize_bug_categories(alluredir: str,
                            baseline: Optional[dict] = None,
                            setup_name: Optional[str] = None) -> None:
    """End-of-session: baseline log_analyzer match, sentinel stamp, categories.json.

    Order matters: log_analyzer matching MUST run before sentinel stamping so
    that just-attached issue links cause the right "known open bug" sentinel
    to be picked instead of "no known open bug".

    setup_name is forwarded to attach_baseline_to_failed_results so platform-
    specific bugs (setup_filters) are correctly scoped. Without it,
    _bug_setup_match returns True for every bug regardless of filters.
    """
    try:
        if baseline is None:
            baseline = load_baseline()
        baseline_attached = attach_baseline_to_failed_results(alluredir, baseline, setup_name=setup_name)
        counts = stamp_known_bug_sentinels(alluredir)
        wrote = write_categories_json(alluredir)
        _log.info(
            "bug_marker: baseline log_analyzer attachments=%d; "
            "stamped %d known-bug + %d unknown failures; categories.json %s",
            baseline_attached, counts["known"], counts["unknown"],

            "written" if wrote else "unchanged",
        )
    except Exception:
        _log.exception("bug_marker: finalize failed (non-fatal)")
