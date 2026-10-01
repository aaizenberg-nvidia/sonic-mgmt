"""Shared pytest run policies for NVOS workloads and Valgrind lifecycle steps.

Policies apply directly to local pytest runs. In MARS, enabled policies are
cached by session ID so separately launched pytest processes inherit them.
"""

import logging
import pytest

logger = logging.getLogger(__name__)

_DUT_REBOOT_CACHE_PREFIX = "no_dut_reboot"
_NO_TECHSUPPORT_ON_FAIL_CACHE_PREFIX = "no_techsupport_on_fail"


def _get_cache_key(config: pytest.Config, cache_prefix: str) -> str:
    """Return a MARS session cache key, or an empty string for local runs."""
    session_id = config.getoption("--session_id", default=None)
    return f"{cache_prefix}/{session_id}" if session_id else ""


def _resolve_boolean_policy(config: pytest.Config, option_name: str, cache_prefix: str) -> bool:
    """Return the effective value of a boolean run policy.

    The command-line value applies directly to the current process. When a
    MARS session ID is present, an enabled policy is cached and remains enabled
    for subsequent pytest processes in that session.

    Args:
        config: Active pytest configuration.
        option_name: Registered boolean command-line option.
        cache_prefix: Namespace for the MARS session cache entry.

    Returns:
        The effective policy value for this pytest process.
    """
    cache_key = _get_cache_key(config, cache_prefix)
    enabled = bool(config.getoption(option_name))
    if cache_key:
        # Local runs have no cache key and therefore never read or write persistent policy state.
        if enabled:
            config.cache.set(cache_key, True)
        else:
            enabled = bool(config.cache.get(cache_key, False))

    return enabled


def pytest_addoption(parser: pytest.Parser):
    """Register command-line options shared by NVOS pytest entry points."""
    parser.addoption(
        "--no-dut-reboot",
        action="store_true",
        default=False,
        help=(
            "Skip tests marked as able to reboot, reset, or power-cycle the DUT; "
            "post-checkers keep non-reboot recovery enabled and stop the run if a reboot is required"
        ),
    )
    parser.addoption(
        "--no-techsupport-on-fail",
        action="store_true",
        default=False,
        help="Skip generating techsupport on test failure",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Configure policies before test collection and fixture initialization."""
    config = session.config
    config._no_dut_reboot = _resolve_boolean_policy(config, "--no-dut-reboot", _DUT_REBOOT_CACHE_PREFIX)
    config._no_techsupport_on_fail = _resolve_boolean_policy(config, "--no-techsupport-on-fail", _NO_TECHSUPPORT_ON_FAIL_CACHE_PREFIX)

    if config._no_dut_reboot:
        # Preserve an existing marker expression while excluding reboot-capable tests.
        markexpr = (config.option.markexpr or "").strip()
        config.option.markexpr = f"({markexpr}) and not dut_reboot" if markexpr else "not dut_reboot"
