import json
import logging
import os
import re
import subprocess
import time
from functools import cache
from typing import List, Optional

import pytest

from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import (
    LinkHealthConsts,
    NvosConst,
    PeerPortConsts,
)
from ngts.nvos_tools.infra.RegressionConfigurations import Configurations
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.nvos_tools.Devices.IbDevice import RosalindSwitch
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.ngts_types import DevicesT
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


def is_link_health_supported(devices: DevicesT) -> bool:
    """Link health is Rosalind NVL-6 only (QM4 product family).

    Gate on ``asic_type == NVL6`` (what Rosalind reports), not a QM4 string.
    Not supported on IB, GB, or Juliet NVL5.
    """
    return (
        isinstance(devices.dut, RosalindSwitch) and
        devices.dut.asic_type == NvosConst.NVL6
    )


def skip_if_link_health_not_supported(devices: DevicesT) -> None:
    if not is_link_health_supported(devices):
        pytest.skip("Link health is only supported on VR Rosalind / NVL-6 (QM4) switches")


@cache  # per-engine image capability; re-probe only matters if the image changes mid-session
def is_link_health_feature_available(dut_engine) -> bool:
    """True when the running image exposes the aggregate link-health command."""
    output = dut_engine.run_cmd(
        "nv show interface link-health -o json 2>&1",
        validate=False,
        print_output=False,
    )
    if not output or not output.strip():
        return False
    lowered = output.lower()
    if "error:" in lowered or "invalid" in lowered or "unknown" in lowered:
        return False
    try:
        json.loads(output)
        return True
    except json.JSONDecodeError:
        return False


def skip_if_link_health_feature_unavailable(devices: DevicesT, dut_engine) -> None:
    skip_if_link_health_not_supported(devices)
    if not is_link_health_feature_available(dut_engine):
        pytest.skip("Link health CLI is not available on this image yet")


def _is_empty_field(value) -> bool:
    return value in (None, '', {}, [])


def _parse_link_health_state(health_output: dict) -> Optional[str]:
    state = health_output.get(LinkHealthConsts.STATE)
    if isinstance(state, dict):
        state = next(iter(state.keys()), None)
    return state


def read_link_health_cli_state(port: Port) -> str:
    """Return the current link-health CLI state for a port."""
    health_output = OutputParsingTool.parse_json_str_to_dictionary(
        port.interface.link.health.show()
    ).get_returned_value()
    return _parse_link_health_state(health_output)


def verify_healthy_link_health_block(health_output: dict, context: str = "") -> None:
    prefix = f"{context}: " if context else ""
    state = _parse_link_health_state(health_output)
    assert state == LinkHealthConsts.STATE_HEALTHY, (
        f"{prefix}expected state={LinkHealthConsts.STATE_HEALTHY}, got {state!r}"
    )
    reason = health_output.get(LinkHealthConsts.ATTENTION_TRIGGER_CODE)
    since = health_output.get(LinkHealthConsts.SINCE)
    assert _is_empty_field(reason), (
        f"{prefix}attention-reason should be empty when healthy, got {reason!r}"
    )
    assert _is_empty_field(since), (
        f"{prefix}since should be empty when healthy, got {since!r}"
    )


def verify_link_health_state_valid(state: str, context: str = "") -> None:
    prefix = f"{context}: " if context else ""
    assert state in LinkHealthConsts.VALID_STATES, (
        f"{prefix}unexpected link-health state {state!r}"
    )


def verify_link_health_summary_on_nvl_ports(devices: DevicesT, dut_engine) -> None:
    """CLI-02: Link Health present on NVL ports in `nv show interface` JSON."""
    with allure.step("Verify link-health on NVL ports in nv show interface output"):
        raw = Port.show_interface(dut_engine)
        output = json.loads(raw)
        nvl_ports = [
            p for p in devices.dut.all_nvl_ports_list
            if p in output and
            output[p].get(IbInterfaceConsts.TYPE) == IbInterfaceConsts.NVL
        ]
        assert nvl_ports, "No NVLink (type=nvl) ports found in show interface output"
        for port_name in nvl_ports:
            port_doc = output[port_name]
            link_block = port_doc.get(LinkHealthConsts.LINK, port_doc.get('link', {}))
            health_block = link_block.get(LinkHealthConsts.HEALTH)
            if health_block is None:
                health_block = port_doc.get(LinkHealthConsts.LINK_HEALTH)
            assert health_block is not None, (
                f"Port {port_name}: link health block missing from show interface JSON"
            )
            state = health_block.get(LinkHealthConsts.STATE)
            if isinstance(state, dict):
                state = next(iter(state.keys()), None)
            verify_link_health_state_valid(state, port_name)
            if state == LinkHealthConsts.STATE_HEALTHY:
                verify_healthy_link_health_block(health_block, port_name)


def verify_link_health_drill_in(port: Port) -> None:
    """CLI-03/04: per-port `nv show interface <id> link health`."""
    with allure.step(f"Verify link health drill-in for {port.name}"):
        health_output = OutputParsingTool.parse_json_str_to_dictionary(
            port.interface.link.health.show()
        ).get_returned_value()
        state = health_output.get(LinkHealthConsts.STATE)
        if isinstance(state, dict):
            state = next(iter(state.keys()), None)
        verify_link_health_state_valid(state, port.name)
        if state == LinkHealthConsts.STATE_HEALTHY:
            verify_healthy_link_health_block(health_output, port.name)


def _extract_aggregate_health_block(row):
    """Return the nested link-health block from a `nv show interface link-health` row.

    Actual shape: ``{"link": {"health": {"state", "attention-trigger-code", "since"}}}``.
    Also tolerates a direct ``{"health": {...}}`` fallback. Returns the block dict or None.
    """
    if not isinstance(row, dict):
        return None
    link_block = row.get(LinkHealthConsts.LINK, row.get('link', {}))
    if isinstance(link_block, dict):
        health = link_block.get(LinkHealthConsts.HEALTH)
        if isinstance(health, dict):
            return health
    health = row.get(LinkHealthConsts.HEALTH)
    return health if isinstance(health, dict) else None


def verify_link_health_aggregate(dut_engine) -> None:
    """CLI-01: `nv show interface link-health` aggregate table.

    Row shape: ``{<port>: {"link": {"health": {state, attention-trigger-code, since}}}}``.
    """
    with allure.step("Verify nv show interface link-health aggregate"):
        output = OutputParsingTool.parse_json_str_to_dictionary(
            dut_engine.run_cmd("nv show interface link-health -o json")
        ).get_returned_value()
        assert output, "link-health aggregate returned empty output"
        checked = 0
        for port_name, row in output.items():
            health_block = _extract_aggregate_health_block(row)
            if not health_block:
                # Blank/non-NVL rows are allowed (HLD 2.3.1.1(d)); nothing to assert.
                logger.debug(
                    f"aggregate row {port_name}: no link.health block (blank/non-NVL row); skipping"
                )
                continue
            state = health_block.get(LinkHealthConsts.STATE)
            if isinstance(state, dict):
                state = next(iter(state.keys()), None)
            if not state:
                logger.debug(f"aggregate row {port_name}: empty link-health state; skipping")
                continue
            verify_link_health_state_valid(state, port_name)
            checked += 1
            if state == LinkHealthConsts.STATE_ATTENTION:
                verify_attention_link_health_block(health_block, port_name)
            elif state == LinkHealthConsts.STATE_HEALTHY:
                verify_healthy_link_health_block(health_block, port_name)
        assert checked > 0, (
            "link-health aggregate returned rows but none exposed a link.health block; "
            f"format may have changed. Sample row: {next(iter(output.items()), None)!r}"
        )


def verify_link_health_interface_block(port: Port) -> None:
    """CLI-02 (per-port): `nv show interface <port>` exposes the link.health block."""
    with allure.step(f"Verify link.health block in nv show interface {port.name}"):
        output = OutputParsingTool.parse_show_interface_output_to_dictionary(
            port.interface.show()
        ).get_returned_value()
        link_block = output.get(LinkHealthConsts.LINK, output.get('link', {})) or {}
        health_block = link_block.get(LinkHealthConsts.HEALTH)
        if health_block is None:
            health_block = output.get(LinkHealthConsts.LINK_HEALTH)
        assert health_block is not None, (
            f"Port {port.name}: link.health block missing from nv show interface {port.name}"
        )
        state = health_block.get(LinkHealthConsts.STATE)
        if isinstance(state, dict):
            state = next(iter(state.keys()), None)
        verify_link_health_state_valid(state, port.name)
        if state == LinkHealthConsts.STATE_HEALTHY:
            verify_healthy_link_health_block(health_block, port.name)


def verify_link_health_link_nested(port: Port) -> None:
    """CLI (nested): `nv show interface <port> link` exposes the health sub-block."""
    with allure.step(f"Verify health block in nv show interface {port.name} link"):
        output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            port.interface.link.show()
        ).get_returned_value()
        health_block = output.get(LinkHealthConsts.HEALTH)
        assert health_block is not None, (
            f"Port {port.name}: health block missing from nv show interface {port.name} link"
        )
        state = health_block.get(LinkHealthConsts.STATE)
        if isinstance(state, dict):
            state = next(iter(state.keys()), None)
        verify_link_health_state_valid(state, port.name)
        if state == LinkHealthConsts.STATE_HEALTHY:
            verify_healthy_link_health_block(health_block, port.name)


def verify_link_health_read_only(port: Port, dut_engine) -> None:
    """Feature is show-only: there must be no `nv set` path for link health."""
    with allure.step(f"Verify link-health is read-only (no nv set) for {port.name}"):
        out = dut_engine.run_cmd(
            f"nv set interface {port.name} link health state healthy 2>&1",
            validate=False,
            print_output=False,
        )
        lowered = (out or '').lower()
        assert any(
            token in lowered
            for token in ('error', 'invalid', 'unknown', 'unrecognized', 'does not exist')
        ), (
            f"Port {port.name}: nv set on link health should be rejected (read-only feature); "
            f"got: {out[:200]!r}"
        )


def verify_link_health_all_switch_endpoints(port: Port, dut_engine) -> None:
    """HLD VTP §4.1: one helper validating every switch-side link-health surface.

      1. ``nv show interface <port>``            -> link.health block present
      2. ``nv show interface <port> link``       -> nested health block present
      3. ``nv show interface <port> link health``-> drill-in state
      4. ``nv show interface link-health``       -> aggregate table
      5. no ``nv set`` path                      -> feature is read-only
    """
    with allure.step(f"Verify all switch link-health endpoints for {port.name}"):
        verify_link_health_interface_block(port)
        verify_link_health_link_nested(port)
        verify_link_health_drill_in(port)
        verify_link_health_aggregate(dut_engine)
        verify_link_health_read_only(port, dut_engine)


def verify_link_health_hidden_on_platform(dut_engine) -> None:
    """CLI-07 / NEG-01: non-NVL6 platforms must not expose link-health surfaces."""
    with allure.step("Verify link-health is hidden on unsupported platform"):
        aggregate_output = dut_engine.run_cmd(
            "nv show interface link-health -o json 2>&1",
            validate=False,
            print_output=False,
        ) or ''
        lowered = aggregate_output.lower()
        assert (
            "error:" in lowered or
            "invalid" in lowered or
            "unknown" in lowered or
            not aggregate_output.strip()
        ), (
            "link-health aggregate command should be unavailable on unsupported platforms; "
            f"got: {aggregate_output[:200]!r}"
        )

        summary_output = dut_engine.run_cmd(
            "nv show interface -o json 2>&1",
            validate=False,
            print_output=False,
        ) or ''
        try:
            summary_json = json.loads(summary_output)
        except json.JSONDecodeError:
            return
        for port_name, port_doc in summary_json.items():
            link_block = port_doc.get('link', {})
            assert LinkHealthConsts.HEALTH not in link_block, (
                f"Port {port_name}: link.health should be hidden on unsupported platform"
            )
            assert LinkHealthConsts.LINK_HEALTH not in port_doc, (
                f"Port {port_name}: link-health field should be hidden on unsupported platform"
            )


def verify_attention_link_health_block(health_output: dict, context: str = "") -> None:
    prefix = f"{context}: " if context else ""
    state = _parse_link_health_state(health_output)
    assert state == LinkHealthConsts.STATE_ATTENTION, (
        f"{prefix}expected state={LinkHealthConsts.STATE_ATTENTION}, got {state!r}"
    )
    reason = health_output.get(LinkHealthConsts.ATTENTION_TRIGGER_CODE)
    since = health_output.get(LinkHealthConsts.SINCE)
    assert not _is_empty_field(reason), (
        f"{prefix}attention-reason should be populated when in attention, got {reason!r}"
    )
    assert not _is_empty_field(since), (
        f"{prefix}since should be populated when in attention, got {since!r}"
    )


def verify_link_health_na_when_link_down(port: Port, known_bug_id: Optional[int] = None) -> str:
    """Deprecated ST-07 helper: link down was once expected to report n/a.

    The link-health build fix makes link-health sticky across admin down —
    ``healthy`` stays ``healthy``, ``attention`` stays ``attention``, and the
    old n/a-on-down behavior is now a regression. Use
    ``verify_link_health_after_link_down`` for current tests.
    """
    with allure.step(f"Verify link-health when {port.name} is down (deprecated n/a check)"):
        health_output = OutputParsingTool.parse_json_str_to_dictionary(
            port.interface.link.health.show()
        ).get_returned_value()
        state = _parse_link_health_state(health_output)
        if state == LinkHealthConsts.STATE_NA:
            logger.warning(
                f"{port.name}: link-health is n/a when down — this is the pre-fix regression "
                f"(bug {known_bug_id}); post-fix sticky behavior is healthy/attention"
            )
        return state


def verify_link_health_after_link_down(port: Port, baseline_state: str) -> tuple:
    """Verify link-health retains its pre-down value after admin down.

    Returns (observed_state, output). Per the link-health build fix (bug: admin
    down no longer forces ``n/a``), a down link keeps whatever link-health it had
    just before it went down, so the value tells you the pre-down status:
      - down from ``healthy``   -> still ``healthy``.
      - down from ``attention`` -> still ``attention`` (reason/since preserved).
      - down from ``n/a``       -> still ``n/a`` (reason/since cleared).

    The state must equal ``baseline_state`` exactly; a change to ``n/a`` from a
    healthy/attention baseline is the regression this asserts against.
    """
    with allure.step(
        f'Verify link-health retains pre-down value on {port.name} (baseline {baseline_state!r})'
    ):
        state, health_output = _read_link_health_state(port)
        assert state == baseline_state, (
            f'Port {port.name}: link-health must retain its pre-down value {baseline_state!r} '
            f'after admin down, got {state!r}; output={health_output!r}'
        )
        if state == LinkHealthConsts.STATE_ATTENTION:
            verify_attention_link_health_block(health_output, port.name)
        elif state == LinkHealthConsts.STATE_HEALTHY:
            verify_healthy_link_health_block(health_output, port.name)
        else:
            reason = health_output.get(LinkHealthConsts.ATTENTION_TRIGGER_CODE)
            since = health_output.get(LinkHealthConsts.SINCE)
            assert _is_empty_field(reason), (
                f'Port {port.name}: attention-trigger-code should be empty for n/a, got {reason!r}'
            )
            assert _is_empty_field(since), (
                f'Port {port.name}: since should be empty for n/a, got {since!r}'
            )
        return state, health_output


def _gnmi_status_prefix(port_name: str) -> str:
    return LinkHealthConsts.GNMI_PREFIX_TEMPLATE.format(port_name=port_name)


def _parse_gnmic_flat_value(output: str, path_fragment: str) -> Optional[str]:
    """Return the last value for a gnmic --format flat line containing ``path_fragment``."""
    if not output:
        return None
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line or path_fragment not in line:
            continue
        if ': ' in line:
            return line.rsplit(': ', 1)[-1].strip().strip('"').strip("'")
        sep_idx = max(line.rfind('='), line.rfind(':'))
        if sep_idx != -1:
            return line[sep_idx + 1:].strip().strip('"').strip("'")
    return None


def _parse_gnmic_flat_status(output: str) -> Optional[str]:
    """Return the last status value from gnmic --format flat output."""
    return _parse_gnmic_flat_value(output, LinkHealthConsts.GNMI_PATH_STATUS)


def _parse_gnmi_link_health_state(output: str) -> dict:
    """Parse status, attention-reason, and attention-since from gnmic flat output."""
    return {
        LinkHealthConsts.STATE: _parse_gnmic_flat_value(output, LinkHealthConsts.GNMI_PATH_STATUS),
        LinkHealthConsts.ATTENTION_TRIGGER_CODE: _parse_gnmic_flat_value(
            output, LinkHealthConsts.GNMI_PATH_ATTENTION_TRIGGER_CODE
        ),
        LinkHealthConsts.SINCE: _parse_gnmic_flat_value(
            output, LinkHealthConsts.GNMI_PATH_ATTENTION_SINCE
        ),
    }


def _is_gnmi_zero_or_empty(value) -> bool:
    if _is_empty_field(value):
        return True
    try:
        return int(str(value).strip()) == 0
    except (TypeError, ValueError):
        return False


def _normalize_attention_reason(value) -> Optional[str]:
    if _is_empty_field(value):
        return None
    return str(value).strip()


def _read_link_health_cli_fields(port: Port) -> dict:
    health_output = OutputParsingTool.parse_json_str_to_dictionary(
        port.interface.link.health.show()
    ).get_returned_value()
    return {
        LinkHealthConsts.STATE: _parse_link_health_state(health_output),
        LinkHealthConsts.ATTENTION_TRIGGER_CODE: health_output.get(LinkHealthConsts.ATTENTION_TRIGGER_CODE),
        LinkHealthConsts.SINCE: health_output.get(LinkHealthConsts.SINCE),
    }


def _assert_gnmi_link_health_parity(
    gnmi_state: dict,
    cli_fields: dict,
    expected_cli_state: str,
    port_name: str,
    context: str = '',
) -> None:
    """Assert gNMI status + reason + since match CLI semantics.

  ``UNHEALTHY`` alone is not accepted for attention: reason and since must
  corroborate the CLI state. For n/a, reason and since must be cleared so
  gNMI does not look like attention.
    """
    prefix = f'{context}: ' if context else ''
    gnmi_status = gnmi_state.get(LinkHealthConsts.STATE)
    gnmi_reason = gnmi_state.get(LinkHealthConsts.ATTENTION_TRIGGER_CODE)
    gnmi_since = gnmi_state.get(LinkHealthConsts.SINCE)
    cli_reason = cli_fields.get(LinkHealthConsts.ATTENTION_TRIGGER_CODE)
    cli_since = cli_fields.get(LinkHealthConsts.SINCE)

    if expected_cli_state == LinkHealthConsts.STATE_NA:
        assert _is_gnmi_zero_or_empty(gnmi_reason), (
            f'{prefix}Port {port_name}: gNMI attention-reason must be 0/empty for n/a; '
            f'non-zero value {gnmi_reason!r} implies attention, not n/a'
        )
        assert _is_gnmi_zero_or_empty(gnmi_since), (
            f'{prefix}Port {port_name}: gNMI attention-since must be 0/empty for n/a, '
            f'got {gnmi_since!r}'
        )
        if _gnmi_value_matches_cli_state(gnmi_status, LinkHealthConsts.STATE_NA):
            return
        if gnmi_status is None:
            # UNSPECIFIED is the YANG default (enum 0); gnmic omits default leaves
            # on the wire (same as `since` at baseline). Absent status + cleared
            # reason/since is the expected n/a encoding.
            logger.info(
                f'{prefix}Port {port_name}: gNMI status leaf absent for CLI n/a; '
                'treating omitted UNSPECIFIED default with cleared reason/since as n/a'
            )
            return
        if _gnmi_value_matches_cli_state(gnmi_status, LinkHealthConsts.STATE_ATTENTION):
            logger.warning(
                f'{prefix}Port {port_name}: gNMI status is UNHEALTHY for CLI n/a — '
                'YANG specifies UNSPECIFIED for n/a; cleared reason/since confirm n/a '
                'semantics but status encoding deviates from model (§18)'
            )
            return
        assert not _gnmi_value_matches_cli_state(gnmi_status, LinkHealthConsts.STATE_HEALTHY), (
            f'{prefix}Port {port_name}: gNMI status is HEALTHY while CLI is n/a '
            f'(false healthy); status={gnmi_status!r}'
        )
        pytest.fail(
            f'{prefix}Port {port_name}: expected gNMI status UNSPECIFIED (or UNHEALTHY with '
            f'cleared reason/since) for CLI n/a, got status={gnmi_status!r} '
            f'(reason={gnmi_reason!r}, since={gnmi_since!r})'
        )

    assert _gnmi_value_matches_cli_state(gnmi_status, expected_cli_state), (
        f'{prefix}Port {port_name}: expected gNMI status semantically matching CLI '
        f'{expected_cli_state!r}, got status={gnmi_status!r} '
        f'(reason={gnmi_reason!r}, since={gnmi_since!r})'
    )

    if expected_cli_state == LinkHealthConsts.STATE_HEALTHY:
        assert _is_gnmi_zero_or_empty(gnmi_reason), (
            f'{prefix}Port {port_name}: gNMI attention-reason should be 0/empty when '
            f'healthy, got {gnmi_reason!r}'
        )
        assert _is_gnmi_zero_or_empty(gnmi_since), (
            f'{prefix}Port {port_name}: gNMI attention-since should be 0/empty when '
            f'healthy, got {gnmi_since!r}'
        )
        return

    if expected_cli_state == LinkHealthConsts.STATE_ATTENTION:
        assert not _is_gnmi_zero_or_empty(gnmi_reason), (
            f'{prefix}Port {port_name}: gNMI status {gnmi_status!r} is ambiguous without '
            f'a non-zero attention-reason; UNHEALTHY alone cannot confirm attention'
        )
        assert _normalize_attention_reason(gnmi_reason) == _normalize_attention_reason(cli_reason), (
            f'{prefix}Port {port_name}: gNMI attention-reason {gnmi_reason!r} does not match '
            f'CLI {cli_reason!r}'
        )
        assert not _is_gnmi_zero_or_empty(gnmi_since), (
            f'{prefix}Port {port_name}: gNMI attention-since should be non-zero when CLI '
            f'since is populated ({cli_since!r}), got {gnmi_since!r}'
        )
        return

    pytest.fail(f'{prefix}Port {port_name}: unsupported expected_cli_state {expected_cli_state!r}')


def _gnmi_value_matches_cli_state(gnmi_value: str, cli_state: str) -> bool:
    """Semantic match between NVUE CLI state and gNMI status enumeration.

    YANG (nvidia-interfaces-infiniband.yang rev 1.0.4): UNSPECIFIED / HEALTHY /
    UNHEALTHY. CLI ``n/a`` maps to UNSPECIFIED; ``attention`` maps to UNHEALTHY.
    """
    if gnmi_value is None:
        return False
    normalized = str(gnmi_value).strip().upper().replace('-', '_').replace('/', '_')
    if cli_state == LinkHealthConsts.STATE_HEALTHY:
        return normalized in {LinkHealthConsts.GNMI_STATUS_HEALTHY, 'HEALTH'}
    if cli_state == LinkHealthConsts.STATE_ATTENTION:
        return normalized in {LinkHealthConsts.GNMI_STATUS_UNHEALTHY, 'ATTENTION', 'UNHEALTH'}
    if cli_state == LinkHealthConsts.STATE_NA:
        return normalized in {
            LinkHealthConsts.GNMI_STATUS_UNSPECIFIED,
            'UNSPEC',
            'NA',
            'N_A',
            'NOT_APPLICABLE',
            'NOTAPPLICABLE',
            'NONE',
        }
    return False


def is_gnmi_server_enabled(dut_engine) -> bool:
    """True when gNMI server is enabled on the DUT (skip gNMI checks otherwise)."""
    output = dut_engine.run_cmd(
        'nv show system gnmi-server -o json 2>&1',
        validate=False,
        print_output=False,
    )
    if not output or not output.strip():
        return False
    try:
        doc = json.loads(output)
    except json.JSONDecodeError:
        return False
    state = doc.get(GnmiConsts.GNMI_STATE_FIELD, doc)
    if isinstance(state, dict):
        state = state.get('operational', state.get(GnmiConsts.GNMI_STATE_FIELD))
    return str(state).lower() == GnmiConsts.GNMI_STATE_ENABLED


def _gnmic_runner_engine():
    """Return the engine that should run ``gnmic`` (sonic-mgmt player), if available."""
    from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit

    engines = TestToolkit.engines
    if engines is not None and hasattr(engines, 'sonic_mgmt'):
        return engines.sonic_mgmt
    return None


# Official openconfig installer (drops the binary into /usr/local/bin). Pin to the
# same gnmic version GnmiClient installs so player tooling stays consistent.
GNMIC_INSTALL_CMD = (
    'sudo bash -c "$(curl -sL https://get-gnmic.openconfig.net)" -- -v 0.38.2'
)


def _gnmic_version_ok(client) -> bool:
    """True when ``gnmic version`` runs on the client's engine (binary present)."""
    try:
        out, err, _ = client.cmd_runner.run_cmd_in_process('gnmic version')
    except Exception as exc:  # noqa: BLE001
        logger.warning(f'gnmic version check failed: {exc!r}')
        return False
    combined = f'{out}\n{err}'.lower()
    return 'not found' not in combined and 'version' in combined


def _install_gnmic_on_engine(engine, where) -> bool:
    """Best-effort install of gnmic on the sonic-mgmt player. Returns success."""
    if engine is None:
        return False
    with allure.step(f'gnmic not found on {where}; attempting auto-install'):
        logger.info(f'Installing gnmic on {where} via openconfig installer')
        try:
            engine.run_cmd(GNMIC_INSTALL_CMD, validate=False)
        except Exception as exc:  # noqa: BLE001 - install is best-effort
            logger.warning(f'gnmic auto-install on {where} raised: {exc!r}')
            return False
    return True


def try_build_gnmi_client(dut_ip, username, password, engine=None):
    """Build a GnmiClient, auto-installing gnmic on the player if it is missing.

    ``gnmic`` runs on ``engines.sonic_mgmt`` when available (same pattern as
    ``ngts/tests_nvos/system/gnmi/helpers.py``), otherwise on the local pytest
    host. Do not pass the DUT engine: the switch has gnmic installed but does
    not expose the xmlrpc command runner the framework expects.

    If gnmic is not on the player, this first tries to install it (the player
    already has internet for pip). Only when the binary is still unusable after
    the install attempt does it return None, so a genuine tooling/connectivity
    gap degrades to CLI-only verification instead of failing the whole test.
    """
    from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient

    if engine is None:
        engine = _gnmic_runner_engine()
    if engine is not None:
        where = f'sonic-mgmt ({getattr(engine, "ip", engine)})'
    else:
        where = 'local pytest host'
    try:
        client = GnmiClient(
            dut_ip,
            GnmiConsts.GNMI_DEFAULT_PORT,
            username,
            password,
            verify_tools_installed=False,
            engine=engine,
        )
        if _gnmic_version_ok(client):
            return client
        # gnmic missing -> try to provision it on the player, then re-check.
        if _install_gnmic_on_engine(engine, where) and _gnmic_version_ok(client):
            logger.info(f'gnmic installed successfully on {where}')
            return client
        logger.warning(
            f'gnmic unavailable on {where} and auto-install did not succeed; '
            'skipping gNMI link-health checks'
        )
        return None
    except Exception as exc:  # noqa: BLE001 - tooling gap must never fail the CLI test
        logger.warning(f'Could not initialize gNMI client on {where}, skipping gNMI checks: {exc!r}')
        return None


def gnmi_link_health_state_once(
    gnmi_client,
    port_name: str,
    username: str,
    password: str,
) -> dict:
    """One-shot gNMI read of link-health status, attention-reason, and attention-since."""
    from ngts.tests_nvos.system.gnmi.constants import GnmiMode

    out, err, _, _ = gnmi_client.gnmic_subscribe(
        _gnmi_status_prefix(port_name),
        LinkHealthConsts.GNMI_PATH_STATE,
        GnmiMode.ONCE,
        flat=True,
        username=username,
        password=password,
        skip_cert_verify=True,
        cmd_time=15,
        wait_till_done=True,
    )
    combined = f'{out}\n{err}'
    assert 'error' not in combined.lower() or 'subscription' in combined.lower(), (
        f'gNMI get failed for {port_name} link-health state: {combined[:500]!r}'
    )
    return _parse_gnmi_link_health_state(combined)


def gnmi_link_health_status_once(gnmi_client, port_name: str, username: str, password: str) -> Optional[str]:
    """One-shot gNMI read of link-health status for a port."""
    return gnmi_link_health_state_once(gnmi_client, port_name, username, password).get(
        LinkHealthConsts.STATE
    )


def verify_gnmi_link_health_matches_cli(
    gnmi_client,
    port_name: str,
    expected_cli_state: str,
    username: str,
    password: str,
    context: str = '',
    port: Optional[Port] = None,
    cli_health_fields: Optional[dict] = None,
) -> None:
    prefix = f'{context}: ' if context else ''
    step = (
        f'{prefix}Verify gNMI link-health (status, attention-reason, attention-since) '
        f'matches CLI {expected_cli_state!r} for {port_name}'
    )
    with allure.step(step):
        if cli_health_fields is None:
            if port is None:
                pytest.fail(
                    f'{prefix}Port {port_name}: cli_health_fields or port is required for '
                    'full gNMI link-health parity checks'
                )
            cli_health_fields = _read_link_health_cli_fields(port)
        gnmi_state = gnmi_link_health_state_once(gnmi_client, port_name, username, password)
        logger.info(
            f'{prefix}Port {port_name} link-health parity: CLI={cli_health_fields!r}, '
            f'gNMI={gnmi_state!r}'
        )
        _assert_gnmi_link_health_parity(
            gnmi_state,
            cli_health_fields,
            expected_cli_state,
            port_name,
            context=context,
        )


def start_gnmi_link_health_stream(
    gnmi_client,
    port_name: str,
    username: str,
    password: str,
) -> subprocess.Popen:
    """Keep a STREAM subscription open before a link-state change."""
    from ngts.tests_nvos.system.gnmi.constants import GnmiMode

    _, _, _, process = gnmi_client.gnmic_subscribe(
        _gnmi_status_prefix(port_name),
        LinkHealthConsts.GNMI_PATH_STATE,
        GnmiMode.STREAM,
        flat=True,
        username=username,
        password=password,
        skip_cert_verify=True,
        keep_session_alive=True,
    )
    time.sleep(2)
    return process


def _split_gnmi_stream_samples(combined: str) -> List[str]:
    """Split flat gnmic STREAM output into per-sample blocks (blank-line separated)."""
    return [block for block in re.split(r'\n\s*\n', combined) if block.strip()]


def _gnmi_sample_matches_state(gnmi_state: dict, expected_cli_state: str) -> bool:
    """True if one parsed gNMI sample matches the expected CLI state semantics.

    Attention requires a corroborating non-zero trigger-code (UNHEALTHY status
    alone is ambiguous); healthy only needs the status; n/a needs UNSPECIFIED/
    absent status with cleared trigger-code and since.
    """
    status = gnmi_state.get(LinkHealthConsts.STATE)
    reason = gnmi_state.get(LinkHealthConsts.ATTENTION_TRIGGER_CODE)
    since = gnmi_state.get(LinkHealthConsts.SINCE)
    if expected_cli_state == LinkHealthConsts.STATE_ATTENTION:
        return (
            _gnmi_value_matches_cli_state(status, LinkHealthConsts.STATE_ATTENTION) and
            not _is_gnmi_zero_or_empty(reason)
        )
    if expected_cli_state == LinkHealthConsts.STATE_HEALTHY:
        return _gnmi_value_matches_cli_state(status, LinkHealthConsts.STATE_HEALTHY)
    if expected_cli_state == LinkHealthConsts.STATE_NA:
        na_status = (
            status is None or
            _gnmi_value_matches_cli_state(status, LinkHealthConsts.STATE_NA) or
            _gnmi_value_matches_cli_state(status, LinkHealthConsts.STATE_ATTENTION)
        )
        return na_status and _is_gnmi_zero_or_empty(reason) and _is_gnmi_zero_or_empty(since)
    return _gnmi_value_matches_cli_state(status, expected_cli_state)


def verify_gnmi_stream_includes_cli_state(
    out: str,
    err: str,
    expected_cli_state: str,
    port_name: str,
    context: str = '',
    cli_health_fields: Optional[dict] = None,  # noqa: ARG001 (kept for call-site compat)
) -> None:
    """Assert a closed STREAM streamed at least one sample matching the expected state.

    A STREAM carries multiple samples across transitions, so each per-sample block
    is parsed independently. Collapsing the whole blob into one (last-value-wins)
    state mixes leaves from different samples (e.g. a trailing HEALTHY status with a
    stale attention-since) and yields false mismatches, so it is avoided here.
    """
    from ngts.tests_nvos.system.gnmi.helpers import verify_msg_not_in_out_or_err

    prefix = f'{context}: ' if context else ''
    combined = f'{out}\n{err}'
    verify_msg_not_in_out_or_err('auth fail', out, err)

    parsed_states = [
        _parse_gnmi_link_health_state(block)
        for block in _split_gnmi_stream_samples(combined)
    ]
    if any(_gnmi_sample_matches_state(state, expected_cli_state) for state in parsed_states):
        return

    # Fallback for builds that omit reason/since leaves: accept a bare status-token
    # match for healthy/na. Attention still needs a corroborating trigger-code above.
    if expected_cli_state != LinkHealthConsts.STATE_ATTENTION:
        status_tokens = re.findall(
            r'\b(?:UNSPECIFIED|HEALTHY|UNHEALTHY|ATTENTION|NA|N/A|NOT[_-]?APPLICABLE)\b',
            combined,
            flags=re.IGNORECASE,
        )
        if any(_gnmi_value_matches_cli_state(tok, expected_cli_state) for tok in status_tokens):
            return

    observed = [
        (
            state.get(LinkHealthConsts.STATE),
            state.get(LinkHealthConsts.ATTENTION_TRIGGER_CODE),
            state.get(LinkHealthConsts.SINCE),
        )
        for state in parsed_states
    ]
    pytest.fail(
        f'{prefix}Port {port_name}: gNMI STREAM did not include a sample matching CLI '
        f'{expected_cli_state!r}. Samples (status, trigger-code, since): {observed!r}. '
        f'Output excerpt: {combined[-1500:]!r}'
    )


def verify_link_health_down_with_gnmi(
    port: Port,
    gnmi_client,
    stream_process: Optional[subprocess.Popen],
    username: str,
    password: str,
    baseline_state: str,
    close_stream: bool = False,
) -> str:
    """CLI + gNMI parity after admin down. Returns the observed CLI state.

    A down link retains its pre-down link-health value (healthy stays healthy,
    attention stays attention), and gNMI is validated against that observed CLI
    state. Keeps the STREAM subscription open by default so the caller can verify
    link-up recovery.
    """
    observed_state, _ = verify_link_health_after_link_down(port, baseline_state)
    verify_gnmi_link_health_matches_cli(
        gnmi_client,
        port.name,
        observed_state,
        username,
        password,
        context='after link down',
        port=port,
    )
    if close_stream and stream_process is not None:
        out, err = gnmi_client.close_session_and_get_out_and_err(stream_process, delay=2)
        verify_gnmi_stream_includes_cli_state(
            out,
            err,
            observed_state,
            port.name,
            context='stream after link down',
            cli_health_fields=_read_link_health_cli_fields(port),
        )
    return observed_state


def finalize_gnmi_link_health_stream(
    gnmi_client,
    stream_process: subprocess.Popen,
    port_name: str,
    expected_cli_states: List[str],
    delay: int = 2,
) -> None:
    """Close an open STREAM subscription and verify it reported the expected states."""
    with allure.step(
        f'Close gNMI link-health stream on {port_name} and verify transitions'
    ):
        out, err = gnmi_client.close_session_and_get_out_and_err(stream_process, delay=delay)
        for state in expected_cli_states:
            verify_gnmi_stream_includes_cli_state(
                out, err, state, port_name, context=f'stream includes {state!r}',
            )


def verify_link_health_up_with_gnmi(
    port: Port,
    gnmi_client,
    username: str,
    password: str,
) -> None:
    """CLI healthy on link up; optional matching gNMI once read when client is provided."""
    with allure.step(f"Verify link-health is healthy when {port.name} is up"):
        health_output = OutputParsingTool.parse_json_str_to_dictionary(
            port.interface.link.health.show()
        ).get_returned_value()
        verify_healthy_link_health_block(health_output, port.name)
    if gnmi_client is None:
        return
    verify_gnmi_link_health_matches_cli(
        gnmi_client,
        port.name,
        LinkHealthConsts.STATE_HEALTHY,
        username,
        password,
        context='after link up',
        port=port,
    )


PTER_REGISTER = 'PTER'

# PTER error_type_admin values (provisional lab recipe — FW/FAE approval pending)
PTER_ERROR_TYPE_CLEAR = '0x0'
PTER_ERROR_TYPE_BER = '0x2'       # Effective/Raw BER -> attention-reason 4
PTER_ERROR_TYPE_SYMBOL = '0x4'    # Symbol errors -> attention-reason 3/9
# BER magnitude for injection. 0x3 (1e-3) is too aggressive on some ports/ASICs
# (admin-downs the link or has no clean effect); 0x4 (1e-4) reliably trips
# attention in the lab. Bump toward 0x5 if a port needs a gentler magnitude.
PTER_BER_MANTISSA = '0x1'
PTER_BER_EXP = '0x4'


def enable_asic_error_injection(dut_engine) -> None:
    """Enable FAE ASIC error-injection (mandatory before PTER is accepted)."""
    dut_engine.run_cmd(
        'nv action enable fae platform asic error-injection',
        validate=False,
        print_output=False,
    )


def _pter_indexes(local_port) -> str:
    return f'plane_ind=0x0,lp_msb=0x0,pnat=0x0,local_port={local_port},error_page=0x0'


def _pter_cmd(mst_device, local_port, error_type_admin, error_injection_time='0x0',
              ber_mantissa='0x0', ber_exp='0x0') -> str:
    set_params = (
        f'error_type_admin={error_type_admin},error_injection_time={error_injection_time},'
        f'ber_mantissa={ber_mantissa},ber_exp={ber_exp}'
    )
    return (
        f'sudo mlxreg -d {mst_device} -reg_name {PTER_REGISTER} '
        f'--indexes "{_pter_indexes(local_port)}" --set "{set_params}" --yes'
    )


def _pter_get_cmd(mst_device, local_port) -> str:
    return (
        f'sudo mlxreg -d {mst_device} -reg_name {PTER_REGISTER} '
        f'--indexes "{_pter_indexes(local_port)}" --get'
    )


def _mlxreg_reports_error(output: str) -> bool:
    """True only for real mlxreg failures.

    The PTER register echoes field names that contain 'error' (error_type_admin,
    error_page, error_type_cap, error_type_oper, error_injection_time), so a bare
    'error' substring match falsely flags a successful set. Match only genuine
    mlxreg error markers instead.
    """
    if not output:
        return False
    lowered = output.lower()
    error_markers = (
        '-e-',            # mlxreg error prefix
        'failed',
        'not supported',
        'error:',
        'cannot',
        'invalid',
        'bad parameter',
        'no such',
        'permission denied',
    )
    return any(tok in lowered for tok in error_markers)


def read_pter_error_type(dut_engine, mst_device, local_port, sudo_password=None) -> Optional[int]:
    """Read back PTER error_type_admin via mlxreg --get. Returns int or None."""
    get_cmd = _pter_get_cmd(mst_device, local_port)
    if sudo_password:
        output = _run_compute_sudo_cmd(dut_engine, sudo_password, get_cmd)
    else:
        output = dut_engine.run_cmd(get_cmd, validate=False, print_output=False)
    if not output:
        return None
    for line in output.splitlines():
        if 'error_type_admin' in line:
            match = re.search(r'0x[0-9a-fA-F]+|\b\d+\b', line.split('error_type_admin', 1)[1])
            if match:
                try:
                    return int(match.group(0), 0)
                except ValueError:
                    return None
    return None


def clear_pter_injection(dut_engine, mst_device, local_port, sudo_password=None) -> None:
    """Disarm PTER injection (error_type_admin=0)."""
    if not mst_device or not local_port:
        return
    cmd = _pter_cmd(mst_device, local_port, PTER_ERROR_TYPE_CLEAR)
    if sudo_password:
        _run_compute_sudo_cmd(dut_engine, sudo_password, cmd)
    else:
        dut_engine.run_cmd(cmd, validate=False, print_output=False)


def arm_pter_attention(dut_engine, mst_device, local_port,
                       error_type_admin=PTER_ERROR_TYPE_BER, sudo_password=None) -> bool:
    """Clear then arm PTER injection, and verify the register write took effect.

    Returns True only when the mlxreg set was accepted (no error) AND a read-back
    shows error_type_admin == the requested value. This distinguishes "register
    write went through" from "write silently ignored", instead of assuming the
    write worked and only checking the downstream CLI.

    Pass ``sudo_password`` when running on a Linux compute host (non-passwordless sudo).
    """
    clear_pter_injection(dut_engine, mst_device, local_port, sudo_password=sudo_password)
    with allure.step(f'Arm PTER on local_port={local_port} (error_type_admin={error_type_admin})'):
        set_cmd = _pter_cmd(
            mst_device, local_port, error_type_admin,
            error_injection_time='0xFFFF', ber_mantissa=PTER_BER_MANTISSA, ber_exp=PTER_BER_EXP,
        )
        if sudo_password:
            set_output = _run_compute_sudo_cmd(dut_engine, sudo_password, set_cmd)
        else:
            set_output = dut_engine.run_cmd(set_cmd, validate=False, print_output=False)
        if _mlxreg_reports_error(set_output):
            logger.warning(
                f'PTER set on local_port={local_port} reported an error; '
                f'register write did NOT go through: {set_output!r}'
            )
            return False

        expected = int(str(error_type_admin), 0)
        read_back = read_pter_error_type(dut_engine, mst_device, local_port, sudo_password=sudo_password)
        if read_back is None:
            logger.warning(
                f'PTER set on local_port={local_port} returned no error, but read-back '
                'could not be parsed; treating register write as unverified'
            )
            return False
        if read_back != expected:
            logger.warning(
                f'PTER write did not take effect on local_port={local_port}: '
                f'set error_type_admin={expected:#x}, read back {read_back:#x} '
                '(register write went through the CLI but was not applied)'
            )
            return False
        logger.info(
            f'PTER write confirmed on local_port={local_port}: error_type_admin={read_back:#x}'
        )
        return True


def _read_link_health_state(port):
    health_output = OutputParsingTool.parse_json_str_to_dictionary(
        port.interface.link.health.show()
    ).get_returned_value()
    state = health_output.get(LinkHealthConsts.STATE)
    if isinstance(state, dict):
        state = next(iter(state.keys()), None)
    return state, health_output


def get_port_logical_state(port) -> Optional[str]:
    """Return the port's logical-state (e.g. 'Active', 'Initialize', 'Down')."""
    link_output = OutputParsingTool.parse_json_str_to_dictionary(
        port.interface.link.show()
    ).get_returned_value()
    return link_output.get(IbInterfaceConsts.LINK_LOGICAL_PORT_STATE)


def get_port_physical_state(port) -> Optional[str]:
    """Return the port's physical-state (e.g. 'LinkUp', 'Disabled')."""
    link_output = OutputParsingTool.parse_json_str_to_dictionary(
        port.interface.link.show()
    ).get_returned_value()
    return link_output.get(IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE)


def is_port_ready_for_attention(port) -> tuple:
    """True when the port can accept a first attention induction.

    Prefer ``logical-state == Active``, but lab evidence on 2182 shows PTER can
    also move an admin-up ``Initialize`` / ``LinkUp`` port into attention. The
    port must be healthy before the first trigger. Returns ``(ready, detail)``.
    """
    logical_state = get_port_logical_state(port)
    physical_state = get_port_physical_state(port)
    health_state, _ = _read_link_health_state(port)
    if health_state != LinkHealthConsts.STATE_HEALTHY:
        return False, f'link-health={health_state!r} (need a healthy baseline before attention)'
    if physical_state != IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP:
        return False, (
            f'physical-state={physical_state!r} (need '
            f'{IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP!r} for injection)'
        )
    if logical_state not in (
        IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE,
        IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_INITIALIZE,
    ):
        return False, (
            f'logical-state={logical_state!r} (need Active or Initialize with LinkUp)'
        )
    return True, (
        f'logical-state={logical_state}, physical-state={physical_state}, '
        'link-health=healthy'
    )


def find_active_healthy_nvl_port(dut_engine, candidate_ports):
    """Return the first admin-up healthy NVL port eligible for attention induction.

    Accepts Active or Initialize + LinkUp ports (see ``is_port_ready_for_attention``).
    """
    for port in candidate_ports:
        try:
            ready, detail = is_port_ready_for_attention(port)
        except Exception as exc:  # noqa: BLE001 - never break selection on one bad port
            logger.warning(f'Skipping {getattr(port, "name", port)} during active-port scan: {exc!r}')
            continue
        if ready:
            logger.info(f'Selected active healthy NVL port for attention: {port.name} ({detail})')
            return port
        logger.debug(f'{port.name} not eligible for attention: {detail}')
    logger.warning(
        'No admin-up healthy NVL port found for attention induction '
        '(need Initialize/Active + LinkUp + healthy)'
    )
    return None


def find_n_eligible_nvl_ports(dut_engine, candidate_ports, count=2):
    """Return up to ``count`` distinct admin-up healthy NVL ports for attention."""
    selected = []
    seen_names = set()
    for port in candidate_ports:
        try:
            ready, detail = is_port_ready_for_attention(port)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f'Skipping {getattr(port, "name", port)} during eligible-port scan: {exc!r}'
            )
            continue
        if not ready or port.name in seen_names:
            if not ready:
                logger.debug(f'{port.name} not eligible for attention: {detail}')
            continue
        logger.info(f'Selected eligible NVL port: {port.name} ({detail})')
        selected.append(port)
        seen_names.add(port.name)
        if len(selected) >= count:
            break
    if len(selected) < count:
        logger.warning(
            f'Found {len(selected)}/{count} eligible NVL port(s) for attention induction'
        )
    return selected


def wait_for_link_health_cli_state(port, expected_state, poll_timeout=30, poll_interval=5) -> bool:
    """Poll `nv show interface <port> link health` until it reaches expected_state.

    Returns True if reached within poll_timeout, else False (no assert here).
    """
    deadline = time.time() + poll_timeout
    last_state = None
    while time.time() < deadline:
        last_state, _ = _read_link_health_state(port)
        if last_state == expected_state:
            return True
        time.sleep(poll_interval)
    logger.warning(
        f'{port.name} did not reach link-health state {expected_state!r} within '
        f'{poll_timeout}s (last state={last_state!r})'
    )
    return False


def trigger_link_health_attention(port, dut_engine, error_type_admin=PTER_ERROR_TYPE_BER,
                                  poll_timeout=60, poll_interval=5,
                                  require_healthy_baseline=True) -> bool:
    """Best-effort: drive `port` into link-health attention (or re-arm while in attention).

    Induction order:
      1. If env var NVOS_LINK_HEALTH_ATTENTION_CMD is set, run it (with {port} /
         {port_name} formatting) as the lab-approved induction command.
      2. Otherwise use the provisional PTER mlxreg recipe (error-injection enable
         + clear + arm) resolved via `nv show fae interface`.

    Set ``require_healthy_baseline=False`` for a repeat trigger while the port is
    already in attention (ST-06 sticky-first-capture check).

    On success the injection is cleared afterwards (attention stays sticky until
    link re-init, so a later down/up cycle returns the port to healthy).

    Returns True when attention is present within poll_timeout (first trigger) or
    already/latched after a repeat trigger. Never raises for a tooling gap.
    """
    override_cmd = os.environ.get('NVOS_LINK_HEALTH_ATTENTION_CMD')
    mst_device = None
    local_port = None

    with allure.step(
        f'Trigger link-health attention on {port.name}'
        f'{" (repeat)" if not require_healthy_baseline else ""}'
    ):
        if require_healthy_baseline:
            ready, detail = is_port_ready_for_attention(port)
            if not ready:
                logger.warning(
                    f'Skipping attention induction on {port.name}: {detail}. '
                    'Pick an eligible port (see find_active_healthy_nvl_port)'
                )
                return False
        else:
            state, _ = _read_link_health_state(port)
            if state != LinkHealthConsts.STATE_ATTENTION:
                logger.warning(
                    f'Skipping repeat attention induction on {port.name}: '
                    f'expected attention, got {state!r}'
                )
                return False

        try:
            if override_cmd:
                cmd = override_cmd.format(port=port.name, port_name=port.name)
                logger.info(f'Using NVOS_LINK_HEALTH_ATTENTION_CMD for {port.name}: {cmd}')
                dut_engine.run_cmd(cmd, validate=False, print_output=False)
            else:
                from ngts.tests_nvos.interfaces.ib_phy_recovery.helpers import (
                    get_local_port_and_mst_device_nvlink,
                )
                try:
                    local_port, mst_device = get_local_port_and_mst_device_nvlink(
                        port.name, dut_engine
                    )
                except Exception as exc:  # noqa: BLE001 - provisional path must not break test
                    logger.warning(
                        f'Could not resolve local_port/mst for {port.name}; '
                        f'skipping attention trigger: {exc!r}'
                    )
                    return False
                enable_asic_error_injection(dut_engine)
                write_ok = arm_pter_attention(dut_engine, mst_device, local_port, error_type_admin)
                if not write_ok:
                    logger.warning(
                        f'PTER register write did not take effect on {port.name}; '
                        'not waiting for attention (see arm_pter_attention log above)'
                    )
                    clear_pter_injection(dut_engine, mst_device, local_port)
                    return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'Attention injection failed on {port.name}: {exc!r}')
            if mst_device and local_port:
                clear_pter_injection(dut_engine, mst_device, local_port)
            return False

        if require_healthy_baseline:
            reached = wait_for_link_health_cli_state(
                port, LinkHealthConsts.STATE_ATTENTION, poll_timeout, poll_interval
            )
            if not reached:
                logger.warning(
                    f'{port.name}: PTER write was confirmed applied but link-health never '
                    f'reached attention within {poll_timeout}s (injection had no health effect)'
                )
        else:
            time.sleep(poll_interval)
            state, _ = _read_link_health_state(port)
            reached = state == LinkHealthConsts.STATE_ATTENTION
            if not reached:
                logger.warning(
                    f'{port.name}: repeat injection did not leave port in attention '
                    f'(state={state!r})'
                )
        if mst_device and local_port:
            clear_pter_injection(dut_engine, mst_device, local_port)
        return reached


ATTENTION_FIRST_TRIGGER_ERROR_TYPES = (PTER_ERROR_TYPE_BER, PTER_ERROR_TYPE_SYMBOL)


def trigger_link_health_attention_first(
    port,
    dut_engine,
    error_types=ATTENTION_FIRST_TRIGGER_ERROR_TYPES,
    poll_timeout=90,
    poll_interval=5,
) -> Optional[str]:
    """First-trigger induction that escalates across PTER error types.

    Some DUTs/idle links do not trip on a raw-BER injection (``0x2``) but do on
    a symbol-error injection (``0x4``). Try each error type in order until one
    drives the port into attention. Returns the error type that worked, or
    ``None`` if none did.
    """
    for error_type in error_types:
        logger.info(
            f'{port.name}: attempting first attention trigger with '
            f'error_type_admin={error_type}'
        )
        if trigger_link_health_attention(
            port,
            dut_engine,
            error_type_admin=error_type,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
        ):
            logger.info(
                f'{port.name}: attention tripped with error_type_admin={error_type}'
            )
            return error_type
        # Borderline timing: the injection can trip attention right at/after the
        # poll deadline, so the wait returns False even though the port is now in
        # attention. Re-check the actual state before escalating (a later error
        # type would otherwise bail on the "need a healthy baseline" guard).
        state, _ = _read_link_health_state(port)
        if state == LinkHealthConsts.STATE_ATTENTION:
            logger.info(
                f'{port.name}: attention is present after error_type_admin={error_type} '
                '(late/borderline trip); accepting as success'
            )
            return error_type
        logger.warning(
            f'{port.name}: error_type_admin={error_type} did not trip attention; '
            'escalating to next error type if available'
        )
    return None


def verify_link_health_attention_on_port(port, context=''):
    """Assert CLI shows attention + numeric reason + non-empty since. Returns (reason, since)."""
    prefix = f'{context}: ' if context else ''
    with allure.step(f'{prefix}Verify link-health attention block for {port.name}'):
        state, health_output = _read_link_health_state(port)
        assert state == LinkHealthConsts.STATE_ATTENTION, (
            f'{prefix}Port {port.name}: expected attention, got {state!r}'
        )
        reason = health_output.get(LinkHealthConsts.ATTENTION_TRIGGER_CODE)
        since = health_output.get(LinkHealthConsts.SINCE)
        assert not _is_empty_field(reason), (
            f'{prefix}Port {port.name}: attention-reason should be populated, got {reason!r}'
        )
        assert not _is_empty_field(since), (
            f'{prefix}Port {port.name}: since should be populated, got {since!r}'
        )
        return reason, since


# ST-06: wait ~2 FlexCounter poll cycles before a repeat trigger.
ATTENTION_STICKY_SETTLE_SEC = 15
ATTENTION_STRESS_TRIGGER_COUNT = 3
TOGGLE_STRESS_CYCLES = 6
TOGGLE_STRESS_SETTLE_SEC = 10


def verify_attention_sticky_first_capture(
    port,
    first_reason,
    first_since,
    context='ST-06',
    gnmi_client=None,
    username: str = '',
    password: str = '',
) -> None:
    """Assert attention still shows the first latched reason and since (ST-06)."""
    prefix = f'{context}: ' if context else ''
    with allure.step(
        f'{prefix}Verify {port.name} retains first attention reason/since after repeat trigger'
    ):
        state, health_output = _read_link_health_state(port)
        assert state == LinkHealthConsts.STATE_ATTENTION, (
            f'{prefix}Port {port.name}: expected attention, got {state!r}'
        )
        reason = health_output.get(LinkHealthConsts.ATTENTION_TRIGGER_CODE)
        since = health_output.get(LinkHealthConsts.SINCE)
        assert _normalize_attention_reason(reason) == _normalize_attention_reason(first_reason), (
            f'{prefix}Port {port.name}: attention-reason should stay at first capture '
            f'{first_reason!r}, got {reason!r}'
        )
        assert since == first_since, (
            f'{prefix}Port {port.name}: since should stay at first capture '
            f'{first_since!r}, got {since!r}'
        )
        if gnmi_client is not None:
            verify_gnmi_link_health_matches_cli(
                gnmi_client,
                port.name,
                LinkHealthConsts.STATE_ATTENTION,
                username,
                password,
                context=f'{prefix}gNMI sticky first capture',
                port=port,
            )


def induce_link_health_attention_first_capture(
    port,
    dut_engine,
    gnmi_client=None,
    username: str = '',
    password: str = '',
    on_attention_latched=None,
):
    """ST-06 phase 1: latch attention once and capture the first reason/since.

    The trigger escalates across PTER error types (BER then SYMBOL) until one
    trips attention. ``on_attention_latched`` is invoked the moment attention is
    confirmed - before the CLI block is validated - so the caller can arm its
    recovery before any assertion can fail.

    Returns ``(working_error_type, first_reason, first_since)``, or
    ``(None, None, None)`` when no error type tripped attention (nothing was
    latched, so the caller owes no attention recovery).
    """
    with allure.step(f'ST-06 first attention capture on {port.name}'):
        working_error_type = trigger_link_health_attention_first(
            port, dut_engine, error_types=ATTENTION_FIRST_TRIGGER_ERROR_TYPES
        )
        if working_error_type is None:
            logger.warning(f'{port.name}: first attention trigger failed')
            return None, None, None
        if on_attention_latched is not None:
            on_attention_latched()

        first_reason, first_since = verify_link_health_attention_on_port(
            port, context='first capture'
        )
        logger.info(
            f'{port.name}: first attention capture reason={first_reason!r} '
            f'since={first_since!r}'
        )
        if gnmi_client is not None:
            verify_gnmi_link_health_matches_cli(
                gnmi_client,
                port.name,
                LinkHealthConsts.STATE_ATTENTION,
                username,
                password,
                context='first capture gNMI',
                port=port,
            )
        return working_error_type, first_reason, first_since


def verify_link_health_attention_sticky_repeat_triggers(
    port,
    dut_engine,
    repeat_error_type,
    first_reason,
    first_since,
    trigger_count=ATTENTION_STRESS_TRIGGER_COUNT,
    settle_sec=ATTENTION_STICKY_SETTLE_SEC,
    gnmi_client=None,
    username: str = '',
    password: str = '',
) -> bool:
    """ST-06 phase 2: re-trigger attention N-1 times; the first capture must stick.

    Attention is already latched when this runs, so the caller owns recovery of
    ``port`` whatever this returns.
    """
    with allure.step(
        f'ST-06 sticky repeat triggers ({trigger_count - 1}x) on {port.name}'
    ):
        for repeat_idx in range(1, trigger_count):
            with allure.step(
                f'Wait {settle_sec}s before repeat attention trigger {repeat_idx}'
            ):
                time.sleep(settle_sec)
            if not trigger_link_health_attention(
                port,
                dut_engine,
                error_type_admin=repeat_error_type,
                require_healthy_baseline=False,
            ):
                logger.warning(
                    f'{port.name}: repeat attention trigger {repeat_idx} failed'
                )
                return False
            verify_attention_sticky_first_capture(
                port,
                first_reason,
                first_since,
                context=f'repeat trigger {repeat_idx}',
                gnmi_client=gnmi_client if repeat_idx == trigger_count - 1 else None,
                username=username,
                password=password,
            )
        return True


def run_link_health_repeated_toggle_stress(
    port,
    test_name,
    devices,
    cycles=TOGGLE_STRESS_CYCLES,
    settle_sec=TOGGLE_STRESS_SETTLE_SEC,
    gnmi_client=None,
    username: str = '',
    password: str = '',
) -> bool:
    """TIM-03 stress: repeated admin down/up on a healthy-baseline port."""
    from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
    from ngts.tests_nvos.interfaces.nvl_port.helpers import toggle_port_state

    with allure.step(
        f'Repeated toggle stress: {cycles} down/up cycles on {port.name}'
    ):
        ready, detail = is_port_ready_for_attention(port)
        if not ready:
            logger.warning(
                f'{port.name} not ready for toggle stress baseline: {detail}'
            )
            return False

        gnmi_check_cycles = {1, max(1, cycles // 2), cycles}
        try:
            for cycle in range(1, cycles + 1):
                cycle_label = f'cycle {cycle}/{cycles}'
                with allure.step(f'{cycle_label}: admin down {port.name}'):
                    toggle_port_state(port, NvosConsts.LINK_STATE_DOWN, test_name, devices)
                    time.sleep(settle_sec)
                    observed_down, _ = verify_link_health_after_link_down(
                        port, LinkHealthConsts.STATE_HEALTHY
                    )
                    if gnmi_client is not None and cycle in gnmi_check_cycles:
                        verify_gnmi_link_health_matches_cli(
                            gnmi_client,
                            port.name,
                            observed_down,
                            username,
                            password,
                            context=f'{cycle_label} down gNMI',
                            port=port,
                        )

                with allure.step(f'{cycle_label}: admin up {port.name}'):
                    toggle_port_state(port, NvosConsts.LINK_STATE_UP, test_name, devices)
                    time.sleep(settle_sec)
                    verify_link_health_up_with_gnmi(
                        port,
                        gnmi_client if cycle in gnmi_check_cycles else None,
                        username,
                        password,
                    )
            return True
        finally:
            # Best-effort restore if the loop is interrupted mid-cycle while
            # the port is still admin-down. Do not mask the original failure.
            try:
                toggle_port_state(port, NvosConsts.LINK_STATE_UP, test_name, devices)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the test result
                logger.warning(
                    f'Failed to restore admin-up on {port.name} after toggle stress: {exc!r}'
                )


def prepare_nvl_link_health_before_trigger(devices, dut_engine, test_name):
    """REL prep: admin-down one NVL port; induce attention on another.

    Returns a context dict when prep succeeds, else ``None`` (caller may skip
    post-trigger health checks). A prep that fails part-way - whether it gives
    up or raises - recovers everything it already mutated, so the caller only
    owns cleanup for a context it actually received.
    """
    if not is_link_health_supported(devices) or not is_link_health_feature_available(dut_engine):
        logger.info('Link-health not on this platform; skipping pre-trigger NVL prep')
        return None
    if not getattr(devices.dut, 'is_nvl', lambda: False)() and devices.dut.switch_type != NvosConst.NVL_SWITCH_TYPE:
        logger.info('Not an NVL switch; skipping pre-trigger link-health prep')
        return None

    from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
    from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
    from ngts.tests_nvos.interfaces.nvl_port.helpers import toggle_port_state

    candidates = RandomizationTool.select_random_ports(
        requested_ports_state=NvosConsts.LINK_STATE_UP,
        num_of_ports_to_select=0,
    ).returned_value or []
    eligible = find_n_eligible_nvl_ports(dut_engine, candidates, count=2)
    if len(eligible) < 2:
        logger.warning('Need 2 eligible NVL ports for link-health trigger prep')
        return None

    down_port, attention_port = eligible[0], eligible[1]
    # Populated from the first mutation on, so a failed prep can still be undone.
    context = {'down_port': down_port, 'attention_port': attention_port}
    prep_done = False
    try:
        with allure.step(
            f'REL prep: admin down {down_port.name}, attention on {attention_port.name}'
        ):
            toggle_port_state(down_port, NvosConsts.LINK_STATE_DOWN, test_name, devices)
            time.sleep(TOGGLE_STRESS_SETTLE_SEC)
            verify_link_health_after_link_down(down_port, LinkHealthConsts.STATE_HEALTHY)

            working_error_type = trigger_link_health_attention_first(attention_port, dut_engine)
            if working_error_type is None:
                logger.warning('Attention prep failed; restoring prepped ports')
                return None

            first_reason, first_since = verify_link_health_attention_on_port(
                attention_port, context='pre-trigger'
            )
            context.update({
                'first_reason': first_reason,
                'first_since': first_since,
                'working_error_type': working_error_type,
            })
            prep_done = True
            return context
    finally:
        if not prep_done:
            recover_nvl_link_health_prep(context, devices, test_name)


def clear_link_health_attention_on_port(port, devices, test_name) -> None:
    """Best-effort bounce of a port to clear a latched attention.

    A no-op unless the port currently reports ``attention``, so callers can run
    it unconditionally in cleanup without disturbing an untouched port.
    """
    from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
    from ngts.tests_nvos.interfaces.nvl_port.helpers import toggle_port_state

    try:
        state, _ = _read_link_health_state(port)
        if state != LinkHealthConsts.STATE_ATTENTION:
            logger.info(f'{port.name} is {state!r}; no latched attention to clear')
            return
        with allure.step(f'Clear latched attention on {port.name} via admin down/up'):
            toggle_port_state(port, NvosConsts.LINK_STATE_DOWN, test_name, devices)
            time.sleep(TOGGLE_STRESS_SETTLE_SEC)
            toggle_port_state(port, NvosConsts.LINK_STATE_UP, test_name, devices)
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the test result
        logger.warning(f'Failed to clear attention on {port.name}: {exc!r}')


def recover_nvl_link_health_prep(context, devices, test_name) -> None:
    """Best-effort restore of every port mutated by prepare_nvl_link_health_*.

    Brings the admin-down port back up and clears a still-latched attention on
    the attention port, so a flow that aborts before the reboot/upgrade/reset
    naturally clears them does not leak dirty state into the next test.
    """
    if not context:
        return
    from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
    from ngts.tests_nvos.interfaces.nvl_port.helpers import toggle_port_state

    down_port = context.get('down_port')
    if down_port is not None:
        with allure.step(f'Recover link-health prep: admin up {down_port.name}'):
            try:
                toggle_port_state(down_port, NvosConsts.LINK_STATE_UP, test_name, devices)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the test result
                logger.warning(
                    f'Failed to restore admin-up on {down_port.name} after link-health prep: {exc!r}'
                )

    attention_port = context.get('attention_port')
    if attention_port is not None:
        clear_link_health_attention_on_port(attention_port, devices, test_name)


def verify_nvl_link_health_after_factory_reset(context, devices, dut_engine) -> None:
    """REL-01: factory reset wipes config — NVL ports should return to healthy."""
    if context is None:
        return
    with allure.step('REL-01: verify link-health after factory reset'):
        verify_link_health_summary_on_nvl_ports(devices, dut_engine)
        verify_link_health_aggregate(dut_engine)
        for port in (context['down_port'], context['attention_port']):
            state, health_output = _read_link_health_state(port)
            assert state == LinkHealthConsts.STATE_HEALTHY, (
                f'Post factory-reset {port.name}: expected healthy baseline, '
                f'got {state!r}; output={health_output!r}'
            )
            verify_healthy_link_health_block(health_output, port.name)


def prepare_nvl_link_health_before_upgrade(devices, dut_engine, test_name):
    """REL-05/REL-06 prep before upgrade or ISSU.

    Admin-downs one eligible NVL port and induces attention on another.
    Shared by ``test_downgrade_upgrade`` and ISSU positive flows.
    """
    return prepare_nvl_link_health_before_trigger(devices, dut_engine, test_name)


def verify_nvl_link_health_after_upgrade(context, devices, dut_engine) -> None:
    """REL-05/REL-06: after upgrade/ISSU, prepared ports must be healthy.

    Stale attention must be cleared; both the pre-down and pre-attention ports
    report ``healthy`` with empty ``attention-trigger-code`` / ``since``.
    Shared by ``test_downgrade_upgrade`` and ISSU positive flows.
    """
    if context is None:
        return
    with allure.step('REL-05/REL-06: verify link-health after upgrade/ISSU'):
        verify_link_health_summary_on_nvl_ports(devices, dut_engine)
        verify_link_health_aggregate(dut_engine)
        for port in (context['down_port'], context['attention_port']):
            state, health_output = _read_link_health_state(port)
            assert state == LinkHealthConsts.STATE_HEALTHY, (
                f'Post upgrade/ISSU {port.name}: expected healthy baseline, '
                f'got {state!r}; output={health_output!r}'
            )
            verify_healthy_link_health_block(health_output, port.name)


def verify_nvl_link_health_after_reboot(context, devices, dut_engine, test_name) -> None:
    """REL-01: reboot clears sticky attention; admin-down may or may not persist."""
    if context is None:
        return
    from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
    from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
    from ngts.tests_nvos.interfaces.nvl_port.helpers import toggle_port_state

    down_port = context['down_port']
    attention_port = context['attention_port']

    with allure.step('REL-01: verify link-health after reboot'):
        verify_link_health_summary_on_nvl_ports(devices, dut_engine)

        att_state, att_output = _read_link_health_state(attention_port)
        assert att_state == LinkHealthConsts.STATE_HEALTHY, (
            f'Post-reboot {attention_port.name}: attention should clear on link '
            f're-init, got {att_state!r}; output={att_output!r}'
        )
        verify_healthy_link_health_block(att_output, attention_port.name)

        intf_show = OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
            Port.show_interface(dut_engine)
        ).get_returned_value()
        admin_state = intf_show.get(down_port.name, {}).get(IbInterfaceConsts.LINK_STATE)
        if admin_state == NvosConsts.LINK_STATE_DOWN:
            verify_link_health_after_link_down(down_port, LinkHealthConsts.STATE_HEALTHY)
            toggle_port_state(down_port, NvosConsts.LINK_STATE_UP, test_name, devices)
            time.sleep(TOGGLE_STRESS_SETTLE_SEC)
            verify_link_health_up_with_gnmi(down_port, None, '', '')
        else:
            logger.info(
                f'{down_port.name} is admin up after reboot; verifying healthy baseline'
            )
            state, health_output = _read_link_health_state(down_port)
            assert state == LinkHealthConsts.STATE_HEALTHY, (
                f'Post-reboot {down_port.name}: expected healthy, got {state!r}'
            )
            verify_healthy_link_health_block(health_output, down_port.name)


def verify_peer_port_link_health_show(peer_port_interface) -> None:
    """PP visibility: peer-port link health drill-in when feature is available."""
    with allure.step(
        f"Verify peer-port link health for {peer_port_interface.peer_port_name}"
    ):
        health_output = OutputParsingTool.parse_json_str_to_dictionary(
            peer_port_interface.peer_port.link.health.show()
        ).get_returned_value()
        state = health_output.get(LinkHealthConsts.STATE)
        if isinstance(state, dict):
            state = next(iter(state.keys()), None)
        verify_link_health_state_valid(state, peer_port_interface.peer_port_name)
        if state == LinkHealthConsts.STATE_HEALTHY:
            verify_healthy_link_health_block(health_output, peer_port_interface.peer_port_name)


def resolve_switch_port_for_peer_port(peer_port_interface):
    """Return (switch Port, peer-port show dict) via associated-switch-port."""
    peer_port_show = OutputParsingTool.parse_json_str_to_dictionary(
        peer_port_interface.peer_port.show()
    ).get_returned_value()
    switch_port_name = peer_port_show.get(PeerPortConsts.ASSOCIATED_SWITCH_PORT)
    if not switch_port_name:
        raise ValueError(
            f'peer-port {peer_port_interface.peer_port_name}: '
            f'missing {PeerPortConsts.ASSOCIATED_SWITCH_PORT!r} in show output'
        )
    return Port(switch_port_name), peer_port_show


def find_active_peer_switch_pair_for_attention(peer_port_obj):
    """Pick a peer-port whose associated switch port is Active + healthy."""
    for peer_intf in peer_port_obj.get_list_of_ports():
        try:
            switch_port, _ = resolve_switch_port_for_peer_port(peer_intf)
            ready, detail = is_port_ready_for_attention(switch_port)
            if ready:
                logger.info(
                    f'Selected peer/switch pair for GPU attention: '
                    f'{peer_intf.peer_port_name} <-> {switch_port.name} ({detail})'
                )
                return peer_intf, switch_port
            logger.debug(
                f'Skipping {peer_intf.peer_port_name} <-> {switch_port.name}: {detail}'
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f'Skipping peer-port {getattr(peer_intf, "peer_port_name", peer_intf)}: {exc!r}'
            )
    logger.warning(
        'No Active + healthy peer/switch pair found for GPU-side attention induction'
    )
    return None, None


def _read_peer_port_link_health_state(peer_port_interface):
    health_output = OutputParsingTool.parse_json_str_to_dictionary(
        peer_port_interface.peer_port.link.health.show()
    ).get_returned_value()
    return _parse_link_health_state(health_output), health_output


def wait_for_peer_port_link_health_cli_state(
    peer_port_interface,
    expected_state,
    poll_timeout=30,
    poll_interval=5,
) -> bool:
    """Poll `nv show peer-port <gpu> link health` until expected_state."""
    deadline = time.time() + poll_timeout
    last_state = None
    peer_name = peer_port_interface.peer_port_name
    while time.time() < deadline:
        last_state, _ = _read_peer_port_link_health_state(peer_port_interface)
        if last_state == expected_state:
            return True
        time.sleep(poll_interval)
    logger.warning(
        f'peer-port {peer_name} did not reach link-health state {expected_state!r} within '
        f'{poll_timeout}s (last state={last_state!r})'
    )
    return False


def verify_peer_port_link_health_attention(peer_port_interface, context=''):
    """Assert peer-port CLI shows attention + numeric reason + non-empty since."""
    prefix = f'{context}: ' if context else ''
    peer_name = peer_port_interface.peer_port_name
    with allure.step(f'{prefix}Verify peer-port link-health attention for {peer_name}'):
        state, health_output = _read_peer_port_link_health_state(peer_port_interface)
        assert state == LinkHealthConsts.STATE_ATTENTION, (
            f'{prefix}peer-port {peer_name}: expected attention, got {state!r}'
        )
        reason = health_output.get(LinkHealthConsts.ATTENTION_TRIGGER_CODE)
        since = health_output.get(LinkHealthConsts.SINCE)
        assert not _is_empty_field(reason), (
            f'{prefix}peer-port {peer_name}: attention-reason should be populated, got {reason!r}'
        )
        assert not _is_empty_field(since), (
            f'{prefix}peer-port {peer_name}: since should be populated, got {since!r}'
        )
        return reason, since


def verify_switch_peer_link_health_independence(peer_port_interface, switch_port, context='PP-07'):
    """GPU-side attention on peer-port; associated switch port must stay healthy."""
    prefix = f'{context}: ' if context else ''
    peer_name = peer_port_interface.peer_port_name
    with allure.step(
        f'{prefix}Verify peer attention on {peer_name} while {switch_port.name} stays healthy'
    ):
        verify_peer_port_link_health_attention(peer_port_interface, context=context)
        switch_state, health_output = _read_link_health_state(switch_port)
        assert switch_state == LinkHealthConsts.STATE_HEALTHY, (
            f'{prefix}switch port {switch_port.name} should stay healthy when only the '
            f'GPU peer filter trips, got {switch_state!r}; output={health_output!r}'
        )
        verify_healthy_link_health_block(health_output, switch_port.name)


def verify_peer_link_health_independent_of_switch_attention(
    peer_port_interface, switch_port, context='PP-07'
):
    """PP-07 (switch->peer direction): switch attention must not reach the peer.

    Switch and peer link-health come from separate feeds (COUNTERS_DB via SAI vs
    GPU_COUNTERS via NMX-T-lite), so a switch-side attention latch must not
    mirror onto the GPU peer. The peer stays ``healthy`` while its own feed is
    live, or ``n/a`` while the NMX-T-lite CSV column is missing - never
    ``attention``, which would only be legal from a GPU-side trigger.

    Returns the observed ``(peer_state, peer_health_output)``.
    """
    prefix = f'{context}: ' if context else ''
    peer_name = peer_port_interface.peer_port_name
    with allure.step(
        f'{prefix}Verify peer {peer_name} does not mirror attention from {switch_port.name}'
    ):
        verify_link_health_attention_on_port(switch_port, context=context)
        peer_state, peer_output = _read_peer_port_link_health_state(peer_port_interface)
        verify_link_health_state_valid(peer_state, peer_name)
        assert peer_state != LinkHealthConsts.STATE_ATTENTION, (
            f'{prefix}peer-port {peer_name}: switch-side attention on {switch_port.name} '
            f'must not propagate to the GPU peer path, got {peer_state!r}; '
            f'output={peer_output!r}'
        )
        if peer_state == LinkHealthConsts.STATE_HEALTHY:
            verify_healthy_link_health_block(peer_output, peer_name)
        return peer_state, peer_output


def _load_gpu_local_port_map() -> dict:
    raw = os.environ.get(LinkHealthConsts.GPU_LOCAL_PORT_MAP_ENV, '').strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        logger.warning(f'Invalid JSON in {LinkHealthConsts.GPU_LOCAL_PORT_MAP_ENV!r}')
        return {}


def _make_compute_ssh_engine(node):
    from infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

    return LinuxSshEngine(node['ip_address'], node['username'], node['password'])


def _run_compute_sudo_cmd(compute_engine, password, cmd) -> str:
    """Run a sudo command on a Linux compute host (password via stdin)."""
    inner = cmd[len('sudo '):] if cmd.startswith('sudo ') else cmd
    wrapped = f"printf '%s\\n' '{password}' | sudo -S -p '' {inner}"
    return compute_engine.run_cmd(wrapped, validate=False, print_output=False)


def _pick_mst_pciconf_device(compute_engine, password) -> Optional[str]:
    """Return the first MST pciconf device path on a compute node."""
    output = _run_compute_sudo_cmd(
        compute_engine, password, 'mst start 2>&1; mst status 2>&1'
    )
    if not output:
        return None
    for line in output.splitlines():
        if 'pciconf' not in line:
            continue
        for token in line.split():
            if token.startswith('/dev/mst/'):
                return token
    return None


def _gpu_attention_format_kwargs(peer_port_interface, switch_port) -> dict:
    peer_name = peer_port_interface.peer_port_name
    switch_name = switch_port.name
    return {
        'peer_port': peer_name,
        'peer_port_name': peer_name,
        'switch_port': switch_name,
        'switch_port_name': switch_name,
        'port': switch_name,
        'port_name': switch_name,
    }


def _resolve_gpu_attention_target(peer_port_interface, switch_port, setup_name):
    """Resolve (compute_engine, password, mst_device, local_port) for GPU mlxreg."""
    fmt = _gpu_attention_format_kwargs(peer_port_interface, switch_port)
    peer_name = peer_port_interface.peer_port_name

    override_cmd = os.environ.get(LinkHealthConsts.GPU_ATTENTION_INDUCTION_ENV)
    if override_cmd:
        return None, None, None, None, override_cmd.format(**fmt)

    port_map = _load_gpu_local_port_map()
    entry = port_map.get(peer_name) or port_map.get(peer_name.lower())
    nodes = Configurations.compute_nodes_per_system.get(setup_name) or []

    if entry:
        target_ip = entry.get('ip')
        mst_device = entry.get('mst')
        local_port = entry.get('local_port')
        node = next((n for n in nodes if n.get('ip_address') == target_ip), None)
        if not node and len(nodes) == 1:
            node = nodes[0]
        if node and mst_device and local_port:
            return _make_compute_ssh_engine(node), node['password'], mst_device, str(local_port), None

    for node in nodes:
        engine = _make_compute_ssh_engine(node)
        mst_device = _pick_mst_pciconf_device(engine, node['password'])
        if not mst_device:
            continue
        local_port = None
        if entry:
            local_port = entry.get('local_port')
        if local_port:
            return engine, node['password'], mst_device, str(local_port), None

    return None, None, None, None, None


def trigger_gpu_side_link_health_attention(
    peer_port_interface,
    switch_port,
    setup_name,
    error_type_admin=PTER_ERROR_TYPE_BER,
    poll_timeout=30,
    poll_interval=5,
) -> bool:
    """Induce attention on the GPU peer path via mlxreg on a compute node.

    Polls ``nv show peer-port <gpu> link health`` for attention (not the switch
    port). Switch-side health is expected to remain healthy (PP-07).

    Lab hooks:
      - ``NVOS_LINK_HEALTH_GPU_ATTENTION_CMD``: full shell command on compute node
      - ``NVOS_LINK_HEALTH_GPU_LOCAL_PORT_MAP``: JSON
        ``{"gpu238": {"ip": "...", "mst": "/dev/mst/...", "local_port": "75"}}``

    Returns True when peer-port attention appears within poll_timeout.
    """
    peer_name = peer_port_interface.peer_port_name
    compute_engine = None
    password = None
    mst_device = None
    local_port = None

    with allure.step(f'Trigger GPU-side link-health attention for peer-port {peer_name}'):
        compute_engine, password, mst_device, local_port, override_cmd = _resolve_gpu_attention_target(
            peer_port_interface, switch_port, setup_name
        )

        try:
            if override_cmd:
                if compute_engine is None:
                    nodes = Configurations.compute_nodes_per_system.get(setup_name) or []
                    if not nodes:
                        logger.warning('No compute nodes configured for GPU attention command')
                        return False
                    node = nodes[0]
                    compute_engine = _make_compute_ssh_engine(node)
                    password = node['password']
                logger.info(
                    f'Using {LinkHealthConsts.GPU_ATTENTION_INDUCTION_ENV} for {peer_name}: '
                    f'{override_cmd}'
                )
                _run_compute_sudo_cmd(compute_engine, password, override_cmd)
            elif compute_engine and mst_device and local_port:
                logger.info(
                    f'GPU mlxreg on {peer_name}: mst={mst_device}, local_port={local_port}'
                )
                clear_pter_injection(
                    compute_engine, mst_device, local_port, sudo_password=password
                )
                if not arm_pter_attention(
                    compute_engine,
                    mst_device,
                    local_port,
                    error_type_admin=error_type_admin,
                    sudo_password=password,
                ):
                    logger.warning(
                        f'GPU PTER write did not take effect for peer-port {peer_name}'
                    )
                    clear_pter_injection(
                        compute_engine, mst_device, local_port, sudo_password=password
                    )
                    return False
            else:
                logger.warning(
                    f'No GPU-side attention induction path for peer-port {peer_name}. '
                    f'Set {LinkHealthConsts.GPU_ATTENTION_INDUCTION_ENV} or '
                    f'{LinkHealthConsts.GPU_LOCAL_PORT_MAP_ENV} (see link_health_helpers docstring).'
                )
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'GPU-side attention injection failed for {peer_name}: {exc!r}')
            if compute_engine and mst_device and local_port:
                clear_pter_injection(
                    compute_engine, mst_device, local_port, sudo_password=password
                )
            return False

        reached = wait_for_peer_port_link_health_cli_state(
            peer_port_interface,
            LinkHealthConsts.STATE_ATTENTION,
            poll_timeout,
            poll_interval,
        )
        if mst_device and local_port and compute_engine:
            clear_pter_injection(
                compute_engine, mst_device, local_port, sudo_password=password
            )
        if not reached:
            logger.warning(
                f'peer-port {peer_name}: GPU injection ran but link-health never '
                f'reached attention within {poll_timeout}s'
            )
        return reached
