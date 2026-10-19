#!/usr/bin/env python3
"""Tag the last sonic-mgmt commit confirmed working in regression, per NOS.

Run after a regression campaign for a given NOS/OS-release has passed, to mark
the sonic-mgmt commit it ran against. Tag format: <nos>-<version>-<date>, date as
day-month-year with no separators, e.g. sonic-202505_RC-17092026

Usage:
    python ngts/performance_tests/scripts/tag_last_working_regression.py --nos sonic --version 202505_RC
    python ngts/performance_tests/scripts/tag_last_working_regression.py --nos cumulus --version 5.10.0 \
        --commit <sha> --push
"""
import argparse
import datetime
import logging
import os
import subprocess
import sys

logger = logging.getLogger()

TAG_DATE_FORMAT = "%d%m%Y"
GIT_TIMEOUT_SECONDS = 60


def _find_checkout_root(start_dir):
    current = start_dir
    while not os.path.exists(os.path.join(current, ".git")):
        parent = os.path.dirname(current)
        if parent == current:
            raise RuntimeError(f"Could not locate the sonic-mgmt checkout root above {start_dir}.")
        current = parent
    return current


SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
REPO_ROOT = _find_checkout_root(SCRIPT_DIR)


def run_git(args):
    """Run a git command inside the sonic-mgmt checkout and return its output, raising on failure."""
    full_args = ["git", "-c", f"safe.directory={REPO_ROOT}"] + args
    try:
        result = subprocess.run(full_args, cwd=REPO_ROOT, capture_output=True, text=True,
                                timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_SECONDS}s")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def build_tag_name(nos, version, date=None):
    date = date or datetime.date.today()
    return f"{nos}-{version}-{date.strftime(TAG_DATE_FORMAT)}"


def tag_exists_locally(tag_name):
    return bool(run_git(["tag", "-l", tag_name]))


def tag_exists_on_remote(tag_name, remote):
    return bool(run_git(["ls-remote", "--tags", remote, f"refs/tags/{tag_name}"]))


def create_tag(tag_name, commit, message):
    run_git(["tag", "-a", tag_name, commit, "-m", message])
    logger.info(f"Created local tag {tag_name} at {commit}")


def push_tag(tag_name, remote):
    try:
        run_git(["push", remote, tag_name])
    except RuntimeError:
        run_git(["tag", "-d", tag_name])
        logger.error(f"Push failed; removed local tag {tag_name} so it can be retried.")
        raise
    logger.info(f"Pushed tag {tag_name} to {remote}")


def confirm(prompt):
    return input(f"{prompt} [y/N]: ").strip().lower() == "y"


def init_parser():
    parser = argparse.ArgumentParser(
        description="Create (and optionally push) a git tag marking the last sonic-mgmt "
                    "commit confirmed working in regression, for a given NOS.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--nos", required=True, help="NOS this tag applies to, e.g. sonic, cumulus, dvs.")
    parser.add_argument("--version", required=True,
                        help="OS release/version that passed regression, e.g. 202505_RC.")
    parser.add_argument("--commit", default="HEAD", help="Commit-ish to tag (default: HEAD).")
    parser.add_argument("--message", default=None, help="Extra note to include in the tag message.")
    parser.add_argument("--remote", default="origin", help="Remote to push the tag to (default: origin).")
    parser.add_argument("--push", action="store_true", help="Push the tag to --remote after creating it.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt (e.g. for CI use).")
    parser.add_argument("--log-level", default=logging.INFO, help="Log verbosity.")
    arguments, unknown = parser.parse_known_args()
    if unknown:
        raise Exception(f"unknown argument(s): {unknown}")
    return arguments


def main():
    args = init_parser()
    logger.setLevel(args.log_level)
    logger.addHandler(logging.StreamHandler())

    try:
        commit_sha = run_git(["rev-parse", "--verify", f"{args.commit}^{{commit}}"])
    except RuntimeError:
        logger.error(f"'{args.commit}' does not resolve to a commit. Refusing to tag it.")
        sys.exit(1)
    tag_name = build_tag_name(args.nos, args.version)

    try:
        run_git(["check-ref-format", f"refs/tags/{tag_name}"])
    except RuntimeError:
        logger.error(f"'{tag_name}' is not a valid git tag name (check --nos/--version for invalid characters).")
        sys.exit(1)

    if tag_exists_locally(tag_name):
        logger.error(f"Tag {tag_name} already exists locally. Refusing to overwrite.")
        sys.exit(1)

    if args.push and tag_exists_on_remote(tag_name, args.remote):
        logger.error(f"Tag {tag_name} already exists on remote '{args.remote}'. Refusing to overwrite.")
        sys.exit(1)

    message = f"Last working version in regression for {args.nos}, version {args.version}."
    if args.message:
        message += f"\n{args.message}"

    logger.info(f"About to tag {commit_sha} ({args.commit}) as {tag_name}")
    if args.push:
        logger.info(f"Tag will be pushed to remote '{args.remote}'")

    action = "Create and push" if args.push else "Create"
    if not args.yes and not confirm(f"{action} tag {tag_name}?"):
        logger.info("Aborted.")
        sys.exit(1)

    create_tag(tag_name, commit_sha, message)

    if args.push:
        push_tag(tag_name, args.remote)
    else:
        logger.info(f"Tag created locally only. Push manually with: git push {args.remote} {tag_name}")


if __name__ == "__main__":
    main()
