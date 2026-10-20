#!/usr/bin/env python3
"""
Allure Nightly Summary Tool - Main Entry Point

This module provides the CLI interface for the Allure Summary tool.

Usage:
    python -m allure_summary --setup-name NVOS_juliet_10_7_145_52 --email user@nvidia.com
    python -m allure_summary --url "https://allure.nvidia.com/.../reports/201/index.html" --email user@nvidia.com
    python -m allure_summary --project nvos-juliet-10-7-145-52 --dry-run
"""

import argparse
import os
import re
import sys
from typing import Optional, Tuple

from ngts.scripts.allure_summary.config import LLM_DEFAULT_MODEL, MailList
from ngts.scripts.allure_summary.models import ReportSummary
from ngts.scripts.allure_summary.allure_client import AllureClient, fetch_report_summary
from ngts.scripts.allure_summary.analyzer import analyze_all_failures
from ngts.scripts.allure_summary.llm_client import LLMGatewayClient, analyze_failures_with_llm
from ngts.scripts.allure_summary.templates import generate_html_email
from ngts.scripts.allure_summary.email_sender import send_email
from ngts.scripts.allure_summary.logger import setup_logger, get_logger
from ngts.scripts.allure_summary.multi_system import fetch_and_aggregate
from ngts.scripts.allure_summary.templates.multi_system_template import generate_multi_system_email
from ngts.nvos_constants.constants_nvos import NvosConst


def get_url_from_verification_file(project_name: str) -> Optional[str]:
    """
    Read the Allure report URL from the verification file.

    The allure_reporter.py script writes the predicted URL to:
    NvosConst.allure_url_folder(project_name)/{project_name}.txt

    Note: The file may use different naming conventions:
    - {project_name}.txt
    - {project_name}-session-reports.txt

    If both exist, we use the MOST RECENTLY MODIFIED one to ensure
    we get the current run's report.

    Args:
        project_name: The Allure project name (e.g., nvos-juliet-10-7-145-52)

    Returns:
        The URL string if file exists and is readable, None otherwise
    """
    logger = get_logger()

    # Try different file naming patterns
    possible_names = [
        f"{project_name}-session-reports.txt",  # Session-based reports
        f"{project_name}.txt",                   # Direct project reports
    ]

    # Find all existing files and their modification times
    found_files = []
    for filename in possible_names:
        file_path = os.path.join(NvosConst.allure_url_folder(project_name), filename)
        if os.path.exists(file_path):
            mtime = os.path.getmtime(file_path)
            found_files.append((file_path, filename, mtime))
            logger.debug(f"Found verification file: {filename} (mtime: {mtime})")

    if not found_files:
        logger.warning(f"No verification file found for project: {project_name}")
        return None

    # Sort by modification time (most recent first) and use the newest
    found_files.sort(key=lambda x: x[2], reverse=True)
    best_file_path, best_filename, best_mtime = found_files[0]

    # Log if we had multiple files
    if len(found_files) > 1:
        from datetime import datetime
        times = [(f, datetime.fromtimestamp(m).strftime('%Y-%m-%d %H:%M:%S'))
                 for _, f, m in found_files]
        logger.info(f"Multiple verification files found: {times}")
        logger.info(f"Using most recent: {best_filename}")

    # Read the URL from the best file
    try:
        with open(best_file_path, 'r') as f:
            url = f.read().strip()
            if url:
                logger.info(f"✅ Found URL in verification file ({best_filename}): {url}")
                return url
            else:
                logger.warning(f"Verification file is empty: {best_file_path}")
                return None
    except IOError as e:
        logger.error(f"Failed to read verification file: {e}")
        return None


def parse_allure_url(url: str) -> Tuple[str, Optional[int]]:
    """
    Parse an Allure report URL to extract project name and report ID.

    Examples:
        https://allure.nvidia.com/allure-docker-service/projects/nvos-crocodile-10-245-21-19-session-reports/reports/201/index.html
        -> ('nvos-crocodile-10-245-21-19-session-reports', 201)
    """
    pattern = r'/projects/([^/]+)/reports/(\d+|latest)'
    match = re.search(pattern, url)

    if not match:
        raise ValueError(f"Could not parse Allure URL: {url}")

    project_name = match.group(1)
    report_id_str = match.group(2)
    report_id = int(report_id_str) if report_id_str.isdigit() else None

    return project_name, report_id


def setup_name_to_project(setup_name: str) -> str:
    """
    Convert MARS setup name to Allure project name.

    Example: NVOS_juliet_10_7_145_52 -> nvos-juliet-10-7-145-52
    """
    return setup_name.lower().replace("_", "-")


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Allure Nightly Regression Summary Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Using MARS setup name (recommended):
  python -m allure_summary --setup-name NVOS_juliet_10_7_145_52 --email user@nvidia.com

  # Using full URL (just copy from browser):
  python -m allure_summary --url "https://allure.nvidia.com/.../reports/201/index.html" --email user@nvidia.com

  # Using project name with specific report:
  python -m allure_summary --project nvos-juliet-10-7-145-52 --report-id 201 --output report.html

  # Dry run (no email sent):
  python -m allure_summary --setup-name NVOS_juliet_10_7_145_52 --email user@nvidia.com --dry-run

  # Using pre-defined mail list:
  python -m allure_summary --setup-name NVOS_juliet_10_7_145_52 --mail-list CORE_TEAM

  # With AI-powered analysis (requires LLM Gateway credentials):
  export LLM_GATEWAY_TOKEN="your-nvauth-token"
  python -m allure_summary --setup-name NVOS_juliet_10_7_145_52 --use-llm --email user@nvidia.com

  # MULTI-SYSTEM MODE: Aggregate results from multiple systems
  python -m allure_summary --multi-system \\
      --setup-name NVOS_juliet_10_7_145_52 \\
      --setup-name NVOS_crocodile_10_245_21_19 \\
      --setup-name NVOS_rosalind_eb1_10 \\
      --email team@nvidia.com

Mail Lists Available:
  CORE_TEAM     - Core verification team
  EXTENDED_TEAM - Extended team with stakeholders
  PLATFORM_TEAM - Platform verification team
  SYSTEM_TEAM   - System verification team
  ALL           - All stakeholders
        """
    )

    # Input options
    input_group = parser.add_argument_group("Input Options")
    input_group.add_argument("--url", help="Full Allure report URL")
    input_group.add_argument("--project", help="Allure project name")
    input_group.add_argument("--setup-name", action="append", dest="setup_names",
                             help="MARS setup name (can be specified multiple times for multi-system mode)")
    input_group.add_argument("--report-id", type=int, help="Specific report ID (default: latest)")

    # Multi-system options
    multi_group = parser.add_argument_group("Multi-System Options")
    multi_group.add_argument("--multi-system", action="store_true",
                             help="Enable multi-system mode to aggregate results from multiple setups")
    multi_group.add_argument("--image-version", help="Override image version for multi-system summary")

    # Email options
    email_group = parser.add_argument_group("Email Options")
    email_group.add_argument("--email", help="Comma-separated list of email recipients")
    email_group.add_argument("--mail-list", choices=[m.name for m in MailList] + ["SKIP"],
                             help="Use a pre-defined mail distribution list (SKIP = no email)")
    email_group.add_argument("--dry-run", action="store_true", help="Don't send email, just preview")

    # AI/LLM options
    llm_group = parser.add_argument_group("AI Analysis Options")
    llm_group.add_argument("--use-llm", action="store_true",
                           help="Enable AI-powered analysis via NVIDIA LLM Gateway")
    llm_group.add_argument("--llm-model", default="azure/openai/gpt-5.4-mini",
                           help="LLM model to use (default: azure/openai/gpt-5.4-mini)")

    # Output options
    output_group = parser.add_argument_group("Output Options")
    output_group.add_argument("--output", help="Save HTML to file")
    output_group.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    # Multi-system mode requires at least 2 setup names
    if args.multi_system:
        if not args.setup_names or len(args.setup_names) < 2:
            parser.error("--multi-system requires at least 2 --setup-name arguments")
    else:
        # Single-system mode: validate that we have either URL, project, or setup-name
        if not args.url and not args.project and not args.setup_names:
            parser.error("Either --url, --project, or --setup-name is required")

    # Validate that we have either email or mail-list or output
    if not args.email and not args.mail_list and not args.output:
        parser.error("Either --email, --mail-list, or --output is required")

    return args


def run_multi_system_mode(args):
    """
    Run in multi-system mode - aggregate results from multiple test systems.

    Args:
        args: Parsed command line arguments
    """
    logger = get_logger()
    logger.info("🔄 Running in MULTI-SYSTEM mode")
    logger.info(f"   Systems: {', '.join(args.setup_names)}")
    logger.info("-" * 40)

    # Fetch and aggregate results from all systems
    multi_summary, all_analyses = fetch_and_aggregate(
        setup_names=args.setup_names,
        image_version=args.image_version
    )

    if not multi_summary.systems:
        logger.error("No systems were successfully fetched. Exiting.")
        sys.exit(1)

    # Log summary
    logger.info("-" * 40)
    logger.info(f"📊 Multi-System Summary:")
    logger.info(f"   Image: {multi_summary.image_version}")
    logger.info(f"   Systems: {multi_summary.system_count}")
    logger.info(f"   Overall: {multi_summary.total_passed}/{multi_summary.total_tests} "
                f"({multi_summary.overall_pass_rate:.1f}%)")
    logger.info(f"   New failures: {multi_summary.new_failure_count}")
    logger.info(f"   Cross-system failures: {len(multi_summary.cross_system_failures)}")

    # Per-system breakdown
    for sys_result in multi_summary.systems:
        s = sys_result.summary
        new_count = len(sys_result.new_failures)
        new_badge = f" 🆕{new_count}" if new_count > 0 else ""
        logger.info(f"   {s.get_status_emoji()} {sys_result.short_name}: "
                    f"{s.pass_rate:.1f}% ({s.passed}/{s.total}){new_badge}")

    # LLM Analysis (if enabled)
    llm_analysis = None
    if args.use_llm:
        logger.info("-" * 40)
        logger.info("🤖 Running AI-powered multi-system analysis...")

        llm_client = LLMGatewayClient(model=args.llm_model)
        if llm_client.is_available():
            from ngts.scripts.allure_summary.llm_client import analyze_multi_system
            llm_analysis = analyze_multi_system(multi_summary, llm_client)
            if llm_analysis:
                logger.info("✅ AI multi-system analysis complete")
            else:
                logger.warning("AI analysis returned no results")
        else:
            logger.warning("LLM credentials not found!")
            logger.info("Continuing without AI analysis...")

    # Generate HTML
    logger.info("-" * 40)
    logger.info("Generating multi-system HTML report...")

    html = generate_multi_system_email(multi_summary, all_analyses, llm_analysis)

    # Save to file if requested
    if args.output:
        try:
            with open(args.output, "w") as f:
                f.write(html)
            logger.info(f"✅ HTML saved to: {args.output}")
        except IOError as e:
            logger.error(f"Failed to save HTML: {e}")

    # Determine recipients (same logic as single-system mode)
    recipients = []
    if args.email:
        if args.email.upper() == "SKIP":
            logger.info("📧 Email not configured - no email will be sent")
        else:
            recipients = [r.strip() for r in args.email.split(",") if r.strip()]
            if recipients:
                logger.info(f"📧 Recipients from config: {', '.join(recipients)}")
    elif args.mail_list:
        if args.mail_list.upper() == "SKIP":
            logger.info("📧 Mail list set to SKIP - no email will be sent")
        else:
            from ngts.scripts.allure_summary.config import get_mail_list
            recipients = get_mail_list(args.mail_list)
            if recipients:
                logger.info(f"Using mail list '{args.mail_list}': {recipients}")
            else:
                logger.warning(f"Mail list '{args.mail_list}' not found or empty")

    # Send email
    if recipients:
        logger.info("-" * 40)
        subject = (f"Multi-System Nightly Summary - {multi_summary.image_version} - "
                   f"{multi_summary.overall_pass_rate:.1f}% Overall")

        success = send_email(
            recipients=recipients,
            subject=subject,
            html_body=html,
            dry_run=args.dry_run
        )

        if not success and not args.dry_run:
            logger.error("Failed to send email")
            sys.exit(1)
    else:
        if not args.output:
            logger.info("📧 No recipients configured - email not sent")

    logger.info("=" * 60)
    logger.info("Done!")
    logger.info("=" * 60)


def main():
    """Main entry point."""
    args = parse_args()

    # Setup logging
    logger = setup_logger(verbose=args.verbose)
    logger.info("=" * 60)
    logger.info("Allure Nightly Summary Tool v1.0.0")
    logger.info("=" * 60)

    # Handle multi-system mode
    if args.multi_system:
        run_multi_system_mode(args)
        return

    # Single-system mode
    # Determine project name and report ID
    if args.url:
        try:
            project_name, report_id = parse_allure_url(args.url)
            logger.info(f"Parsed URL: project={project_name}, report_id={report_id or 'latest'}")
        except ValueError as e:
            logger.error(str(e))
            sys.exit(1)
    elif args.setup_names:
        # Single setup name in non-multi mode
        setup_name = args.setup_names[0]
        base_project_name = setup_name_to_project(setup_name)
        project_name = base_project_name
        report_id = args.report_id
        logger.info(f"Converted setup '{setup_name}' -> base project '{base_project_name}'")

        # Try to get URL from verification file (written by allure_reporter.py)
        # This ensures we get the CURRENT run's report, not the previous one
        if report_id is None:
            verification_url = get_url_from_verification_file(base_project_name)
            if verification_url:
                try:
                    # Parse the full project name and report ID from the URL
                    # This handles the -session-reports suffix correctly
                    parsed_project, report_id = parse_allure_url(verification_url)
                    project_name = parsed_project  # Use the actual project name from URL
                    logger.info(f"Using project '{project_name}' and report #{report_id} from verification file")
                except ValueError:
                    logger.warning("Could not parse verification file URL, will use latest from API")
            else:
                logger.info("No verification file found, will try both project naming patterns")
                # Both patterns are valid - some setups use -session-reports suffix
                # Will be handled in fetch logic below
    else:
        project_name = args.project
        report_id = args.report_id
        logger.info(f"Using project: {project_name}")

    # Fetch report data
    logger.info("-" * 40)
    logger.info("Fetching report data...")

    client = AllureClient()
    summary = fetch_report_summary(client, project_name, report_id)

    if summary.error:
        logger.error(f"Failed to fetch report: {summary.error}")
        sys.exit(1)

    # Log summary
    logger.info("-" * 40)
    logger.info(f"📊 Report Summary:")
    logger.info(f"   {summary}")
    logger.info(f"   Duration: {summary.duration_minutes:.1f} minutes")
    logger.info(f"   Failed/Broken tests: {len(summary.failed_tests)}")

    # Identify new failures (regressions)
    new_failures = [t for t in summary.failed_tests if t.is_new_failure]
    if new_failures:
        logger.info(f"   🆕 NEW FAILURES (regressions): {len(new_failures)}")
        for test in new_failures:
            logger.info(f"      🔴 {test.name}")
    else:
        logger.info(f"   ✅ No new regressions in this build")

    # Show other failures
    other_failures = [t for t in summary.failed_tests if not t.is_new_failure][:5]
    if other_failures:
        logger.info(f"   Other failures ({len([t for t in summary.failed_tests if not t.is_new_failure])} total):")
        for test in other_failures:
            logger.info(f"   [{test.status.upper()}] {test.name}")
        remaining = len([t for t in summary.failed_tests if not t.is_new_failure]) - 5
        if remaining > 0:
            logger.info(f"   ... and {remaining} more")

    # Enrich with known bugs from Confluence
    logger.info("-" * 40)
    logger.info("🔗 Checking known bugs database...")
    try:
        from ngts.scripts.allure_summary.known_bugs import get_known_bugs_database
        from ngts.scripts.allure_summary.models import KnownBugInfo

        bugs_db = get_known_bugs_database()
        known_bug_count = 0
        assigned_count = 0

        for test in summary.failed_tests:
            mapping = bugs_db.find_bug_for_test(test.name)
            if mapping:
                bug_info = KnownBugInfo(
                    bug_id=mapping.bug.bug_id if mapping.bug else "",
                    bug_url=mapping.bug.url if mapping.bug else "",
                    description=mapping.bug.description if mapping.bug else "",
                    assigned_to=mapping.assigned_to,
                    status=mapping.status,
                    notes=mapping.notes
                )
                test.known_bug = bug_info
                if mapping.bug:
                    known_bug_count += 1
                if mapping.assigned_to:
                    assigned_count += 1

        logger.info(f"   Found {known_bug_count} tests with known bugs, {assigned_count} with assignees")
    except Exception as e:
        logger.debug(f"Known bugs enrichment not available: {e}")

    # Analyze failures
    logger.info("-" * 40)
    logger.info("Analyzing failures...")

    analyses = analyze_all_failures(summary.failed_tests)

    # Commit correlation for new failures and new passes (using LLM if available)
    logger.info("-" * 40)
    logger.info("🔗 Correlating with git commits...")
    try:
        from ngts.scripts.allure_summary.commit_analyzer import (
            CommitAnalyzer, find_likely_cause_commits, find_likely_fix_commits, correlate_with_llm,
            NVOS_REPO_PATH, SONIC_MGMT_REPO_PATH
        )
        from ngts.scripts.allure_summary.models import CommitMatch
        from ngts.scripts.allure_summary.llm_client import LLMGatewayClient

        commit_analyzer = CommitAnalyzer()
        nvos_commits = []
        mgmt_commits = []

        if commit_analyzer.is_available() and summary.image_version:
            nvos_commits = commit_analyzer.get_commits_for_version(summary.image_version)
            logger.info(f"   Found {len(nvos_commits)} NVOS commits")

        mgmt_commits = commit_analyzer.get_sonic_mgmt_commits(days=14)
        logger.info(f"   Found {len(mgmt_commits)} sonic-mgmt commits")

        # Check if LLM is available for smart correlation
        llm_client = LLMGatewayClient()
        use_llm = llm_client.is_available()
        summary.ai_available = use_llm
        if use_llm:
            logger.info("   🤖 Using AI for smart commit correlation...")
        else:
            logger.info("   ⚠️ AI not available, using heuristic matching...")

        # Correlate NEW failures with NVOS commits
        new_failures = [t for t in summary.failed_tests if t.is_new_failure]
        if new_failures and nvos_commits:
            logger.info(f"   Correlating {len(new_failures)} new failures with commits...")
            for test in new_failures:
                if use_llm:
                    result = correlate_with_llm(
                        test.name, test.error_message, nvos_commits,
                        NVOS_REPO_PATH, llm_client, is_fix=False
                    )
                    if result:
                        test.likely_cause_commits = [CommitMatch(
                            short_hash=result.commit.short_hash,
                            subject=result.commit.subject[:80],
                            probability=result.probability,
                            reasons=", ".join(result.reasons),
                            repo="nvos"
                        )]
                else:
                    causes = find_likely_cause_commits(test.name, test.error_message, nvos_commits)
                    test.likely_cause_commits = [
                        CommitMatch(
                            short_hash=c.commit.short_hash,
                            subject=c.commit.subject[:80],
                            probability=c.probability,
                            reasons=", ".join(c.reasons),
                            repo=c.repo
                        ) for c in causes
                    ]

        # Correlate newly passing tests with fix commits
        if summary.newly_passed_tests and (nvos_commits or mgmt_commits):
            logger.info(f"   Correlating {len(summary.newly_passed_tests)} new passes with fix commits...")
            for test in summary.newly_passed_tests:
                if use_llm:
                    # Try sonic-mgmt first (test fixes), then nvos
                    result = correlate_with_llm(
                        test.name, "", mgmt_commits,
                        str(SONIC_MGMT_REPO_PATH), llm_client, is_fix=True
                    )
                    if not result and nvos_commits:
                        result = correlate_with_llm(
                            test.name, "", nvos_commits,
                            str(NVOS_REPO_PATH), llm_client, is_fix=True
                        )
                    if result:
                        logger.info(f"      ✅ {test.name[:40]} -> {result.commit.short_hash} ({result.probability * 100:.0f}%)")
                        test.likely_fix_commits = [CommitMatch(
                            short_hash=result.commit.short_hash,
                            subject=result.commit.subject[:80],
                            probability=result.probability,
                            reasons=", ".join(result.reasons),
                            repo=result.repo
                        )]
                    else:
                        logger.debug(f"      ❌ {test.name[:40]} -> No fix found")
                else:
                    fixes = find_likely_fix_commits(test.name, nvos_commits, mgmt_commits)
                    test.likely_fix_commits = [
                        CommitMatch(
                            short_hash=c.commit.short_hash,
                            subject=c.commit.subject[:80],
                            probability=c.probability,
                            reasons=", ".join(c.reasons),
                            repo=c.repo
                        ) for c in fixes
                    ]
    except Exception as e:
        logger.debug(f"Commit correlation not available: {e}")
        import traceback
        logger.debug(traceback.format_exc())

    # LLM Analysis (if enabled)
    llm_analysis = None
    if args.use_llm:
        logger.info("-" * 40)
        logger.info("🤖 Running AI-powered analysis...")

        llm_client = LLMGatewayClient(model=args.llm_model)
        if llm_client.is_available():
            # Try commit correlation analysis if git repo is available
            commit_summary = ""
            try:
                from ngts.scripts.allure_summary.commit_analyzer import CommitAnalyzer
                commit_analyzer = CommitAnalyzer()
                if commit_analyzer.is_available() and summary.image_version:
                    commits = commit_analyzer.get_commits_for_version(summary.image_version)
                    if commits:
                        commit_summary = commit_analyzer.get_commit_summary_for_llm(commits)
            except Exception as e:
                logger.debug(f"Commit summary not available: {e}")

            # Use enhanced analysis with commits if available
            if commit_summary:
                from ngts.scripts.allure_summary.llm_client import analyze_with_commit_correlation
                llm_analysis = analyze_with_commit_correlation(summary, llm_client, commit_summary)
            else:
                llm_analysis = analyze_failures_with_llm(summary, llm_client)

            if llm_analysis:
                logger.info("✅ AI analysis complete")
            else:
                logger.warning("AI analysis returned no results")
        else:
            logger.warning("LLM credentials not found!")
            logger.warning("Set INFERENCE_HUB_API_KEY env var (recommended) or")
            logger.warning("Set LLM_GATEWAY_TOKEN env var (legacy)")
            logger.info("Continuing without AI analysis...")

    # Generate HTML
    logger.info("-" * 40)
    logger.info("Generating HTML report...")

    html = generate_html_email(summary, analyses, llm_analysis)

    # Save to file if requested
    if args.output:
        try:
            with open(args.output, "w") as f:
                f.write(html)
            logger.info(f"✅ HTML saved to: {args.output}")
        except IOError as e:
            logger.error(f"Failed to save HTML: {e}")

    # Determine recipients
    recipients = []
    if args.email:
        # SKIP means no email should be sent (not configured in setup)
        if args.email.upper() == "SKIP":
            logger.info("📧 Email not configured - no email will be sent")
            logger.info("   (Configure 'allure_summary_emails' in .setup file to enable)")
        else:
            recipients = [r.strip() for r in args.email.split(",") if r.strip()]
            if recipients:
                logger.info(f"📧 Recipients from config: {', '.join(recipients)}")
    elif args.mail_list:
        # SKIP means no email should be sent (mail list not configured in setup)
        if args.mail_list.upper() == "SKIP":
            logger.info("📧 Mail list set to SKIP - no email will be sent")
            logger.info("   (Configure 'allure_mail_list' in .setup file to enable)")
        else:
            from ngts.scripts.allure_summary.config import get_mail_list
            recipients = get_mail_list(args.mail_list)
            if recipients:
                logger.info(f"Using mail list '{args.mail_list}': {recipients}")
            else:
                logger.warning(f"Mail list '{args.mail_list}' not found or empty")

    # Send email
    if recipients:
        logger.info("-" * 40)
        subject = f"Nightly Regression Summary - {project_name} - {summary.pass_rate:.1f}% Pass Rate"

        success = send_email(
            recipients=recipients,
            subject=subject,
            html_body=html,
            dry_run=args.dry_run
        )

        if not success and not args.dry_run:
            logger.error("Failed to send email")
            sys.exit(1)
    else:
        if not args.output:
            logger.info("📧 No recipients configured - email not sent")

    logger.info("=" * 60)
    logger.info("Done!")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
