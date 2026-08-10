"""
Black-box UMF churn checks for NVBugs 6152581 / 6152697.

UMF product unit tests cover converter / key-match internals; these helpers
assert the verify-to-close contracts on the DUT:
  - empty IB speed during churn must not log ERROR (6152581 / UMF !191)
  - IB_PORT / ALIAS_PORT_MAP churn must not log Data-index-out-of-range
    (6152697 / UMF !195), and post-settle alias -> IB_PORT rows stay consistent
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Iterable, List, Optional, Sequence, Union

import pytest

from ngts.nvos_constants.constants_nvos import DatabaseConst
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.ResultObj import IssueType, ResultObj
from ngts.tests_nvos.system.gnmi.helpers import get_infiniband_name_from_port_name
from ngts.tools.test_utils import allure_utils as allure

from ngts.tests_nvos.general.telemetry.ib import helpers as ibh
from ngts.tests_nvos.general.telemetry.ib.constants import (
    ALIAS_PORT_MAP_NAME_FIELD,
    CONFIG_DB_IB_PORT_KEY_FMT,
    NVBUG_6152581,
    NVBUG_6152697,
    STATE_DB_IB_PORT_TABLE_KEY_FMT,
    SystemDbCli,
    UMF_AGENT_ERROR_GREP,
    UMF_ALIAS_DATA_INDEX_OUT_OF_RANGE_RE,
    UMF_ALIAS_MAP_SAMPLE_LIMIT,
    UMF_ALIAS_MAP_SETTLE_SEC,
    UMF_CHURN_MARKER_PREFIX,
    UMF_EMPTY_IB_SPEED_PARSE_ERROR,
)

PatternSpec = Union[str, re.Pattern]


def place_umf_churn_marker(engines, label: str) -> str:
    """Inject a unique syslog marker; later asserts scan only lines after it."""
    marker = f"{UMF_CHURN_MARKER_PREFIX}-{label}-{uuid.uuid4().hex[:10]}"
    with allure.step(f"Place UMF churn syslog marker ({label})"):
        # Reuse the existing LA marker injector (logger -p info + quote-safe).
        TestToolkit.add_loganalyzer_marker(engines.dut, marker)
        # Short settle for the logger write; read-time helper does its own HUP.
        time.sleep(2)
        ibh.attach_dict("umf churn marker", {"marker": marker, "label": label})
    return marker


def _compiled_patterns(patterns: Sequence[PatternSpec]) -> List[re.Pattern]:
    compiled: List[re.Pattern] = []
    for pattern in patterns:
        if isinstance(pattern, re.Pattern):
            compiled.append(pattern)
        else:
            compiled.append(re.compile(pattern))
    return compiled


def _syslog_umf_error_lines_since_marker(engines, marker: str) -> List[str]:
    """
    Return nv-umf-agentd ERROR lines that appear after ``marker`` in syslog
    (including rotated files). Empty list when the marker is missing.
    """
    safe_marker = marker.replace("'", "'\\''").replace("\\", "\\\\")
    # zcat -f handles both plain and gzipped syslog; awk starts printing after
    # the marker line; grep keeps agent ERROR lines only.
    cmd = (
        "sudo sh -c \""
        "zcat -f /var/log/syslog.* /var/log/syslog 2>/dev/null | "
        f"awk -v m='{safe_marker}' 'index(\\$0, m) {{p=1; next}} p' | "
        f"grep '{UMF_AGENT_ERROR_GREP}' | grep ' ERROR ' || true"
        "\""
    )
    engines.dut.run_cmd("sudo pkill -HUP rsyslogd || true", validate=False)
    time.sleep(1)
    raw = engines.dut.run_cmd(cmd, validate=False) or ""
    return [line for line in raw.splitlines() if line.strip()]


def assert_no_umf_errors_since(
    engines,
    marker: str,
    patterns: Sequence[PatternSpec],
    bug_id: str,
) -> None:
    """
    Fail if any syslog line after ``marker`` matches one of ``patterns``.

    Patterns match message text only (not Go source line numbers).
    """
    if not marker:
        pytest.fail(f"NVBug {bug_id}: UMF churn marker is empty; cannot scan syslog")

    compiled = _compiled_patterns(patterns)
    with allure.step(f"Assert no UMF ERROR for NVBug {bug_id} since churn marker"):
        lines = _syslog_umf_error_lines_since_marker(engines, marker)
        hits: List[str] = []
        for line in lines:
            for pattern in compiled:
                if pattern.search(line):
                    hits.append(line)
                    break
        ibh.attach_dict(
            f"umf syslog scan NVBug {bug_id}",
            {
                "marker": marker,
                "patterns": [p.pattern for p in compiled],
                "umf_error_line_count": len(lines),
                "hits": hits,
            },
        )
        preview = "\n".join(hits[:20])
        ResultObj(
            not hits,
            (
                f"NVBug {bug_id}: unexpected nv-umf-agentd ERROR after churn "
                f"({len(hits)} hit(s)). First matches:\n{preview}"
            ),
            issue_type=IssueType.PossibleBug,
        ).verify_result()


def assert_no_empty_ib_speed_parse_errors(engines, marker: str) -> None:
    """NVBug 6152581 / UMF !191 — empty speed must not ParseFloat at ERROR."""
    assert_no_umf_errors_since(
        engines,
        marker,
        (re.escape(UMF_EMPTY_IB_SPEED_PARSE_ERROR),),
        NVBUG_6152581,
    )


def assert_no_alias_data_index_errors(engines, marker: str) -> None:
    """NVBug 6152697 / UMF !195 — no positional Data-index-out-of-range ERROR."""
    assert_no_umf_errors_since(
        engines,
        marker,
        (UMF_ALIAS_DATA_INDEX_OUT_OF_RANGE_RE,),
        NVBUG_6152697,
    )


def _normalize_redis_scalar(raw: Optional[str]) -> str:
    return (raw or "").replace('"', "").strip()


def assert_alias_port_map_consistent(
    engines,
    sample_port_names: Iterable[str],
    *,
    settle_sec: int = UMF_ALIAS_MAP_SETTLE_SEC,
    check_state_db: bool = False,
) -> None:
    """
    Post-settle sanity: each NVUE alias resolves via APPL_DB ALIAS_PORT_MAP to an
    Infiniband* name that still has a CONFIG_DB IB_PORT row. Optionally also
    requires STATE_DB IB_PORT_TABLE (Aports after settle; skip for plane-ports
    while the knob is disabled). Does not race mid-UpdateInterfaceMaps rebuilds.
    """
    names = [n for n in sample_port_names if n]
    if not names:
        pytest.fail(
            f"NVBug {NVBUG_6152697}: no port names to sample for ALIAS_PORT_MAP consistency"
        )

    if settle_sec > 0:
        with allure.step(
            f"Settle {settle_sec}s before ALIAS_PORT_MAP consistency (NVBug {NVBUG_6152697})"
        ):
            time.sleep(settle_sec)

    with allure.step(
        f"Assert ALIAS_PORT_MAP consistency for {len(names)} alias(es) (NVBug {NVBUG_6152697})"
    ):
        observations: List[dict] = []
        for port_name in names:
            obs = {
                "port": port_name,
                "ib_name": None,
                "config_ib_port": None,
                "state_ib_port_table": None,
            }
            observations.append(obs)
            with allure.independent_step(f"alias map {port_name}"):
                ib_name = _normalize_redis_scalar(
                    get_infiniband_name_from_port_name(engines.dut, port_name)
                )
                obs["ib_name"] = ib_name
                has_ib_name = bool(ib_name) and ib_name not in ("None", "(nil)")
                # Empty/None alias: verify_result raises AssertionError -> caught
                # by independent_step; loop continues with the next port. The
                # message shows the observed value so logs read correctly on
                # both pass ('name'='Infiniband0') and fail ('name'='').
                ResultObj(
                    has_ib_name,
                    f"{port_name}: ALIAS_PORT_MAP '{ALIAS_PORT_MAP_NAME_FIELD}'={ib_name!r}",
                    issue_type=IssueType.PossibleBug,
                ).verify_result()

                config_key = CONFIG_DB_IB_PORT_KEY_FMT.format(ib_name=ib_name)
                config_row = ibh.db_hgetall(
                    engines, DatabaseConst.CONFIG_DB_NAME, config_key
                )
                obs["config_ib_port"] = bool(config_row)
                ResultObj(
                    bool(config_row),
                    f"{port_name} -> {ib_name}: CONFIG_DB {config_key} rows={len(config_row or {})}",
                    issue_type=IssueType.PossibleBug,
                ).verify_result()

                if check_state_db:
                    state_key = STATE_DB_IB_PORT_TABLE_KEY_FMT.format(ib_name=ib_name)
                    state_row = ibh.db_hgetall(
                        engines, SystemDbCli.STATE_DB, state_key
                    )
                    obs["state_ib_port_table"] = bool(state_row)
                    ResultObj(
                        bool(state_row),
                        f"{port_name} -> {ib_name}: STATE_DB {state_key} rows={len(state_row or {})}",
                        issue_type=IssueType.PossibleBug,
                    ).verify_result()

        ibh.attach_dict(
            f"alias map consistency NVBug {NVBUG_6152697}",
            {"ports": observations},
        )


def sample_port_names(
    port_names: Sequence[str], limit: int = UMF_ALIAS_MAP_SAMPLE_LIMIT
) -> List[str]:
    """Return up to ``limit`` unique non-empty port names for alias sampling."""
    seen = set()
    out: List[str] = []
    for name in port_names:
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= limit:
            break
    return out
