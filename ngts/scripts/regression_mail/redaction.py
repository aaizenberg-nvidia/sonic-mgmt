"""Deterministic setup-name redaction for mail and workbook artifacts."""

from __future__ import annotations

import html
import re
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from ngts.scripts.regression_mail.models import SemanticGroup, SemanticReport
from ngts.scripts.regression_mail.normalization import normalize_text, unique_sorted


UNKNOWN_PLATFORM = "Unknown platform"
_SLM_SETUP = re.compile(r"(?i)(?<![A-Za-z0-9_-])slm-\d+(?![A-Za-z0-9_-])")
_EXPLICIT_SYSTEM_TYPE = re.compile(r"(?i)^ACS-[A-Za-z0-9_.-]+$")


class SetupRedactor:
    """Replace every known setup name with its unambiguous platform."""

    def __init__(
        self,
        setup_platforms: Mapping[str, str],
        setup_testbeds: Optional[Mapping[str, str]] = None,
    ):
        replacements = []
        for setup, platform in setup_platforms.items():
            setup_name = str(setup or "").strip()
            if not setup_name:
                continue
            platform_name = normalize_text(platform)
            if (
                not platform_name or
                platform_name.casefold() == UNKNOWN_PLATFORM.casefold()
            ):
                platform_name = setup_name
            if platform_name.casefold() != setup_name.casefold():
                replacements.append((setup_name, platform_name))
        self.replacements: Tuple[Tuple[str, str], ...] = tuple(
            sorted(replacements, key=lambda item: (-len(item[0]), item[0].casefold()))
        )
        self._platforms = {
            platform.casefold(): platform
            for _, platform in self.replacements
        }
        supplied_testbeds = setup_testbeds or {}
        self._testbeds = {
            str(setup or "").strip().casefold(): _testbed_display(
                setup,
                display,
            )
            for setup, display in supplied_testbeds.items()
            if str(setup or "").strip()
        }
        for setup, platform in self.replacements:
            self._testbeds.setdefault(setup.casefold(), platform)
        prefixes = []
        for setup, platform in self.replacements:
            prefix = _setup_prefix(setup)
            if prefix and platform:
                prefixes.append((prefix, platform))
        self._testbed_prefixes = tuple(
            sorted(
                set(prefixes),
                key=lambda item: (-len(item[0]), item[0].casefold()),
            )
        )

    @property
    def setup_names(self) -> Sequence[str]:
        return tuple(setup for setup, _ in self.replacements)

    def redact(self, value: object) -> str:
        text = str(value or "")
        for setup, platform in self.replacements:
            for candidate in {
                setup,
                html.escape(setup, quote=True),
                html.escape(setup, quote=False),
            }:
                text = re.sub(
                    re.escape(candidate),
                    lambda match, replacement=platform: replacement,
                    text,
                    flags=re.IGNORECASE,
                )
        return text

    def platforms(self, setup_values: Iterable[str]) -> List[str]:
        result = []
        by_setup = {
            setup.casefold(): platform
            for setup, platform in self.replacements
        }
        for value in setup_values:
            normalized = normalize_text(value)
            if not normalized:
                continue
            if (
                normalized.casefold() == UNKNOWN_PLATFORM.casefold() or
                normalized.casefold().startswith(
                    UNKNOWN_PLATFORM.casefold() + "-"
                )
            ):
                continue
            platform = by_setup.get(normalized.casefold())
            if platform is None:
                platform = self._platforms.get(
                    normalized.casefold(),
                    normalized,
                )
            result.append(platform)
        return unique_sorted(result)

    def redact_testbed(self, value: object) -> str:
        normalized = normalize_text(value)
        if not normalized:
            return ""
        if (
            normalized.casefold() == UNKNOWN_PLATFORM.casefold() or
            normalized.casefold().startswith(
                UNKNOWN_PLATFORM.casefold() + "-"
            )
        ):
            return ""
        if _EXPLICIT_SYSTEM_TYPE.fullmatch(normalized):
            return normalized
        replacement = self._testbeds.get(normalized.casefold())
        if replacement is not None:
            return replacement
        folded = normalized.casefold()
        for prefix, platform in self._testbed_prefixes:
            marker = prefix.casefold() + "-"
            if folded.startswith(marker):
                return platform + normalized[len(prefix):]
        redacted = self.redact(normalized)
        if redacted != normalized:
            return redacted
        if re.match(r"(?i)^x86(?:_64|-64)-", normalized):
            return normalized
        return normalized

    def testbeds(self, setup_values: Iterable[str]) -> List[str]:
        values = unique_sorted(
            self.redact_testbed(value)
            for value in setup_values
            if normalize_text(value)
        )
        return [
            value
            for value in values
            if not any(
                other.casefold().startswith(value.casefold() + "-")
                for other in values
                if other != value
            )
        ]

    def redact_test_display(self, value: object) -> str:
        """Redact node IDs and remove parameters that become identical."""

        lines = []
        for raw_line in str(value or "").splitlines():
            line = self.redact(raw_line)
            match = re.match(r"^(.*)\[([^\[\]]+)\]$", line)
            if match:
                parameters = []
                for parameter in match.group(2).split(", "):
                    if parameter not in parameters:
                        parameters.append(parameter)
                line = "{}[{}]".format(
                    match.group(1),
                    ", ".join(parameters),
                )
            lines.append(line)
        return "\n".join(lines)

    def redact_semantic(self, semantic: SemanticReport) -> SemanticReport:
        for group in (
            list(semantic.failure_groups) +
            list(semantic.skip_groups) +
            list(semantic.internal_error_groups)
        ):
            self.redact_group(group)
        return semantic

    def redact_group(self, group: SemanticGroup) -> SemanticGroup:
        group.test_display = self.redact_test_display(group.test_display)
        group.testbeds = self.testbeds(group.testbeds)
        group.comments = self.redact(group.comments)
        group.internal_comments = self.redact(group.internal_comments)
        group.cause_evidence = self.redact(group.cause_evidence)
        return group

    def find_leaks(self, values: Iterable[object]) -> List[str]:
        combined = "\n".join(str(value or "") for value in values).casefold()
        return [
            setup
            for setup, _ in self.replacements
            if any(
                candidate.casefold() in combined
                for candidate in {
                    setup,
                    html.escape(setup, quote=True),
                    html.escape(setup, quote=False),
                }
            )
        ]

    def assert_clean(self, values: Iterable[object], label: str) -> None:
        if self.find_leaks(values):
            raise ValueError("known setup name remained in {}".format(label))


def _testbed_display(setup: object, display: object) -> str:
    setup_name = normalize_text(setup)
    display_name = normalize_text(display)
    if (
        not display_name or
        display_name.casefold() == UNKNOWN_PLATFORM.casefold() or
        display_name.casefold().startswith(
            UNKNOWN_PLATFORM.casefold() + "-"
        )
    ):
        return setup_name
    return display_name


def extract_setup_names(values: Iterable[object]) -> List[str]:
    """Return setup-name forms that are safe to identify from free text."""

    return unique_sorted(
        match.group(0)
        for value in values
        for match in _SLM_SETUP.finditer(str(value or ""))
    )


def _setup_prefix(value: str) -> str:
    match = re.search(r"(?i)(?:_setup|-setup)$", value)
    if match:
        return value[:match.start()].strip()
    for marker in ("sonic_simx_", "sonic_"):
        if value.casefold().startswith(marker):
            return value[len(marker):].strip()
    return ""
