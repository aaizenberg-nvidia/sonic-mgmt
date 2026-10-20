#!/usr/bin/env python3
"""LLM audit of bug attributions on a generated Allure report.

For every failed/broken test in the report that carries an Allure
issue-link (i.e. a known-bug attribution from the bug_marker matcher),
ask the NVIDIA LLM whether the {test_name + error message excerpt +
bug subject} pairing is plausible. Suspected false positives are
recorded in a JSON report and surfaced on stdout so they can be acted
on later (e.g. removed from known_bugs_mappings.json).

Designed to run *inside* the existing MARS "Generate final Allure
report" step, AFTER allure_reporter.py --action generate.

Non-fatal:
- If INFERENCE_HUB_API_KEY (or LLM_GATEWAY_TOKEN) is not set, the
  script logs a warning and exits 0.
- HTTP / parse errors during fetch are logged and skipped.
- Any unexpected exception is caught and logged; exit code stays 0.

Usage:
    python -m ngts.scripts.allure_summary.validate_mappings_with_llm \
        --setup-name NVOS_bm_10_7_148_150
    python -m ngts.scripts.allure_summary.validate_mappings_with_llm \
        --url https://allure.nvidia.com/.../projects/<p>/reports/<N>/index.html
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Iterable, List, Optional, Tuple

# Allow execution from a player where the package is on PYTHONPATH already.
from ngts.scripts.allure_summary.allure_client import AllureClient
from ngts.scripts.allure_summary.config import ALLURE_BASE_URL
from ngts.scripts.allure_summary.llm_client import LLMGatewayClient
from ngts.scripts.allure_summary.logger import setup_logger, get_logger
from ngts.nvos_constants.constants_nvos import NvosConst

DEFAULT_OUTPUT_DIR = "/tmp"
ERROR_EXCERPT_CHARS = 600
BUG_SUBJECT_CHARS = 200
DEFAULT_MAX_FAILURES = 40
URL_RE = re.compile(r"/projects/([^/]+)/reports/(\d+|latest)")


def setup_name_to_project(setup_name: str) -> str:
    return setup_name.lower().replace("_", "-")


def read_predicted_url(project_name: str) -> Optional[str]:
    """Read the URL that allure_reporter.py wrote for this run.

    The reporter writes either <project>.txt or <project>-session-reports.txt.
    We pick the most recently modified one.
    """
    logger = get_logger()
    candidates = [
        f"{project_name}-session-reports.txt",
        f"{project_name}.txt",
    ]
    found: List[Tuple[str, float]] = []
    for name in candidates:
        path = os.path.join(NvosConst.allure_url_folder(project_name), name)
        if os.path.exists(path):
            found.append((path, os.path.getmtime(path)))
    if not found:
        return None
    found.sort(key=lambda kv: kv[1], reverse=True)
    try:
        with open(found[0][0]) as fh:
            return fh.read().strip() or None
    except OSError as exc:
        logger.warning(f"could not read predicted URL file {found[0][0]}: {exc}")
        return None


def parse_url(url: str) -> Tuple[str, Optional[int]]:
    m = URL_RE.search(url)
    if not m:
        raise ValueError(f"unrecognized Allure URL: {url}")
    project = m.group(1)
    rid_raw = m.group(2)
    report_id = int(rid_raw) if rid_raw.isdigit() else None
    return project, report_id


def wait_for_report(client: AllureClient, project: str, report_id: int,
                    retries: int = 6, sleep_s: int = 20) -> bool:
    """Poll the generated report; return True once suites.json is available.

    Allure regeneration can lag behind /generate-report; the new numeric
    report ID is allocated immediately but JSON files appear a few seconds
    later. We retry a handful of times then give up gracefully.
    """
    logger = get_logger()
    for attempt in range(1, retries + 1):
        suites = client.get_suites(project, report_id)
        if suites:
            return True
        logger.debug(f"report {project}/{report_id} not ready (attempt {attempt}); sleeping {sleep_s}s")
        time.sleep(sleep_s)
    return False


def walk_leaves(node: dict) -> Iterable[dict]:
    for c in node.get("children", []) or []:
        if "children" in c:
            yield from walk_leaves(c)
        elif c.get("status"):
            yield c


def issue_links(test_case: dict) -> List[dict]:
    return [
        link for link in (test_case.get("links") or [])
        if link.get("type") == "issue" and (link.get("name") or link.get("url"))
    ]


def excerpt(text: str, limit: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def build_prompt(test_name: str, error_text: str, bug_links: List[dict]) -> str:
    bug_lines = []
    for i, link in enumerate(bug_links, start=1):
        bug_lines.append(
            f"  {i}. name=\"{excerpt(link.get('name') or '', BUG_SUBJECT_CHARS)}\" url={link.get('url') or ''}"
        )
    bug_block = "\n".join(bug_lines) if bug_lines else "  (none)"

    return (
        "You are auditing the bug-attribution pipeline for NVOS regression tests.\n"
        "A failure has been attributed to one or more known open bugs by a strict\n"
        "AND matcher (system_type + exact test_name + error_msg substring).\n\n"
        "Your job: judge whether the attribution is plausible given the test name,\n"
        "the failure message/trace, and the bug subject line.\n\n"
        f"TEST: {test_name}\n\n"
        "FAILURE (message + trace excerpt):\n"
        f"---\n{excerpt(error_text, ERROR_EXCERPT_CHARS)}\n---\n\n"
        "ATTRIBUTED BUGS:\n"
        f"{bug_block}\n\n"
        "Respond in STRICT JSON with no prose around it:\n"
        "{\n"
        "  \"verdict\": \"plausible\" | \"implausible\" | \"unsure\",\n"
        "  \"confidence\": 0.0-1.0,\n"
        "  \"reason\": \"one sentence, <=180 chars\"\n"
        "}\n"
        "Use \"plausible\" if the bug subject describes a defect that could\n"
        "produce the observed failure. Use \"implausible\" if the bug clearly\n"
        "addresses a different symptom (the error_msg substring matched by\n"
        "accident). Use \"unsure\" only when the evidence is too thin.\n"
    )


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def parse_llm_verdict(raw: Optional[str]) -> dict:
    """Best-effort parse of the LLM JSON response."""
    if not raw:
        return {"verdict": "unsure", "confidence": 0.0, "reason": "no LLM response"}
    m = _JSON_RE.search(raw)
    blob = m.group(0) if m else raw
    try:
        parsed = json.loads(blob)
    except (TypeError, json.JSONDecodeError):
        return {
            "verdict": "unsure",
            "confidence": 0.0,
            "reason": f"unparseable LLM output: {excerpt(raw, 120)}",
        }
    verdict = str(parsed.get("verdict") or "unsure").strip().lower()
    if verdict not in {"plausible", "implausible", "unsure"}:
        verdict = "unsure"
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = excerpt(str(parsed.get("reason") or ""), 240)
    return {"verdict": verdict, "confidence": confidence, "reason": reason}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--setup-name", help="MARS setup name (e.g. NVOS_bm_10_7_148_150)")
    p.add_argument("--url", help="Full Allure report URL (overrides --setup-name)")
    p.add_argument("--project", help="Allure project name (alternative to --setup-name)")
    p.add_argument("--report-id", type=int, help="Specific report id (default: predicted/latest)")
    p.add_argument("--output", help="Path to JSON output (default: /tmp/mapping_validation_<project>.json)")
    p.add_argument("--max-failures", type=int, default=DEFAULT_MAX_FAILURES,
                   help=f"Max attributed failures to audit (default {DEFAULT_MAX_FAILURES})")
    p.add_argument("--max-tokens", type=int, default=400,
                   help="Per-call LLM max_tokens (default 400)")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def resolve_target(args) -> Optional[Tuple[str, Optional[int]]]:
    """Return (project, report_id_or_None) or None if we can't figure it out."""
    logger = get_logger()
    if args.url:
        return parse_url(args.url)
    if args.project:
        return args.project, args.report_id
    if not args.setup_name:
        logger.error("one of --setup-name / --project / --url is required")
        return None
    base_project = setup_name_to_project(args.setup_name)
    if args.report_id:
        return base_project, args.report_id
    predicted = read_predicted_url(base_project)
    if predicted:
        try:
            return parse_url(predicted)
        except ValueError as exc:
            logger.warning(f"predicted URL unusable ({exc}); falling back to latest")
    return base_project, None


def main() -> int:
    args = parse_args()
    logger = setup_logger(verbose=args.verbose)
    logger.info("=" * 60)
    logger.info("Mapping audit (LLM) - validate_mappings_with_llm")
    logger.info("=" * 60)

    target = resolve_target(args)
    if not target:
        return 0  # non-fatal
    project, report_id = target

    llm = LLMGatewayClient()
    if not llm.is_available():
        logger.warning("LLM credentials not set (INFERENCE_HUB_API_KEY or LLM_GATEWAY_TOKEN)")
        logger.warning("Skipping mapping audit - exiting 0.")
        return 0

    client = AllureClient(base_url=ALLURE_BASE_URL)

    if report_id is None:
        report_id = client.get_latest_report_id(project)
        if report_id is None:
            # Try the -session-reports suffix automatically
            alt = f"{project}-session-reports"
            report_id = client.get_latest_report_id(alt)
            if report_id is not None:
                project = alt
        if report_id is None:
            logger.warning(f"no reports found for project {project}; skipping")
            return 0

    logger.info(f"target report: {client.get_report_url(project, report_id)}")

    if not wait_for_report(client, project, report_id):
        logger.warning("report did not become available in time; auditing whatever is readable")

    suites = client.get_suites(project, report_id)
    if not suites:
        logger.warning("suites.json unavailable; skipping audit")
        return 0

    leaves = list(walk_leaves(suites))
    failing = [leaf for leaf in leaves if leaf.get("status") in ("failed", "broken")]
    logger.info(f"failed/broken leaves: {len(failing)} / {len(leaves)} total")

    findings: List[dict] = []
    examined = 0
    plausible = implausible = unsure = 0

    for leaf in failing:
        uid = leaf.get("uid")
        if not uid:
            continue
        tc = client.get_test_case(project, report_id, uid)
        if not tc:
            continue
        links = issue_links(tc)
        if not links:
            continue  # no attribution -> nothing to audit
        if examined >= args.max_failures:
            logger.info(f"--max-failures={args.max_failures} reached; stopping early")
            break
        examined += 1
        error_text = "\n".join(filter(None, [tc.get("statusMessage"), tc.get("statusTrace")]))
        prompt = build_prompt(tc.get("name") or "", error_text, links)
        response = llm.chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": "You are a meticulous test triage auditor. Reply with strict JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=args.max_tokens,
            temperature=0.1,
        )
        verdict = parse_llm_verdict(response)
        finding = {
            "test_name": tc.get("name"),
            "status": tc.get("status"),
            "uid": uid,
            "bugs": [
                {"name": link.get("name"), "url": link.get("url")} for link in links
            ],
            "error_excerpt": excerpt(error_text, ERROR_EXCERPT_CHARS),
            "llm_verdict": verdict,
        }
        findings.append(finding)
        v = verdict["verdict"]
        if v == "plausible":
            plausible += 1
        elif v == "implausible":
            implausible += 1
            logger.warning(
                f"IMPLAUSIBLE attribution - {tc.get('name')} -> "
                f"{[(l.get('name') or l.get('url')) for l in links]}: {verdict['reason']}"
            )
        else:
            unsure += 1

    out_path = args.output or os.path.join(
        DEFAULT_OUTPUT_DIR, f"mapping_validation_{project}.json"
    )
    summary = {
        "project": project,
        "report_id": report_id,
        "report_url": client.get_report_url(project, report_id),
        "totals": {
            "leaves": len(leaves),
            "failed_broken": len(failing),
            "with_attribution": examined,
            "plausible": plausible,
            "implausible": implausible,
            "unsure": unsure,
        },
        "findings": findings,
    }
    try:
        with open(out_path, "w") as fh:
            json.dump(summary, fh, indent=2)
        logger.info(f"audit report: {out_path}")
    except OSError as exc:
        logger.warning(f"could not write {out_path}: {exc}")

    logger.info("-" * 60)
    logger.info(
        f"audited {examined} attributed failure(s): "
        f"plausible={plausible}, implausible={implausible}, unsure={unsure}"
    )
    if implausible:
        logger.info("see warnings above for implausible entries to review in known_bugs_mappings.json")
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as exc:  # noqa: BLE001 - non-fatal by design
        get_logger().exception(f"unexpected error: {exc}")
        rc = 0
    sys.exit(rc)
