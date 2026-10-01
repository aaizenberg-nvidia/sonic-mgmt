"""Public command-line interface."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from ngts.scripts.regression_mail.config import Settings
from ngts.scripts.regression_mail.models import ExitCode, RunRequest


_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and send a SONiC regression report email")
    parser.add_argument("--excel", required=True, type=Path, help="Collected regression result workbook")
    parser.add_argument("--version", required=True, help="Exact SONiC image version")
    parser.add_argument("--to", required=True, action="append", help="Primary recipient; repeat for more")
    parser.add_argument("--cc", action="append", default=[], help="CC recipient; repeat for more")
    return parser


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = _build_parser()
    args = parser.parse_args(argv)
    args.version = args.version.strip()
    if not args.version:
        parser.error("--version must not be empty")
    args.to = _validate_addresses(parser, args.to, "--to")
    args.cc = _validate_addresses(parser, args.cc, "--cc")
    return args


def _validate_addresses(
    parser: argparse.ArgumentParser,
    values: Sequence[str],
    option: str,
) -> List[str]:
    result: List[str] = []
    for raw in values:
        address = raw.strip()
        if "\r" in address or "\n" in address or not _EMAIL_PATTERN.fullmatch(address):
            parser.error("invalid {} address: {!r}".format(option, raw))
        if address not in result:
            result.append(address)
    return result


def trigger_from_jenkins(
    version,
    recipients,
    excel_path,
    cc=(),
    runner=None,
):
    """Generate and send the report in the current Jenkins Python process."""

    version = str(version or "").strip()
    if not version or not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("version is missing or invalid")
    to_addresses = _addresses(recipients, "recipients")
    cc_addresses = _addresses(cc, "cc", required=False)
    path = Path(str(excel_path or "").strip()).expanduser()
    if not path.is_file() or not os.access(str(path), os.R_OK):
        raise ValueError(
            "collected-results Excel is not readable: {}".format(path)
        )

    argv = ["--excel", str(path), "--version", version]
    for address in to_addresses:
        argv.extend(["--to", address])
    for address in cc_addresses:
        argv.extend(["--cc", address])
    return main(argv, runner=runner)


def _addresses(value, label, required=True):
    supplied = re.split(r"[,;]", value) if isinstance(value, str) else list(value or ())
    result = []
    for raw in supplied:
        address = str(raw or "").strip()
        if not address:
            continue
        if not _EMAIL_PATTERN.fullmatch(address):
            raise ValueError("invalid {} address: {!r}".format(label, raw))
        if address not in result:
            result.append(address)
    if required and not result:
        raise ValueError("{} must contain at least one address".format(label))
    return result


def main(
    argv: Optional[Sequence[str]] = None,
    runner: Optional[Callable[[RunRequest, Settings], int]] = None,
) -> int:
    args = _parse_args(argv)
    request = RunRequest(
        excel_path=args.excel.expanduser(),
        version=args.version,
        to=tuple(args.to),
        cc=tuple(args.cc),
    )

    try:
        settings = Settings.from_env()
    except (TypeError, ValueError) as error:
        sys.stderr.write("regression mail delivery configuration is invalid: {}\n".format(error))
        return int(ExitCode.DELIVERY)

    if runner is None:
        from ngts.scripts.regression_mail.pipeline import run

        runner = run
    return int(runner(request, settings))
