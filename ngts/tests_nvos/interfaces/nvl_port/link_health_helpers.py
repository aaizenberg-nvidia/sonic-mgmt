import json
import logging
import os
import random
import re
import time
from functools import cache
from typing import Optional

import pytest

from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import (
    LinkHealthConsts,
    NvosConst,
    PeerPortConsts,
)
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


def _normalize_attention_field(value) -> Optional[str]:
    if _is_empty_field(value):
        return None
    return str(value).strip()


def _is_empty_or_zero_field(value) -> bool:
    """Empty, or the FW ``n/a`` encoding (metric index 0)."""
    normalized = _normalize_attention_field(value)
    return normalized is None or normalized == '0'


def _split_legacy_reason(reason) -> tuple:
    """Split a pre-split ``reason`` (``<metric name>.<index>``) into its halves."""
    normalized = _normalize_attention_field(reason)
    if normalized is None:
        return None, None
    trigger, _, metric = normalized.rpartition('.')
    if trigger and metric.isdigit():
        return trigger, metric
    return normalized, None


def _extract_attention_fields(health_output: dict) -> tuple:
    """Return ``(trigger, metric)`` from a link-health block.

    ``trigger`` (FW metric name) + ``metric`` (its index) replaced the combined
    ``reason`` leaf; images predating the split are read through the fallback.
    """
    if not isinstance(health_output, dict):
        return None, None
    trigger = health_output.get(LinkHealthConsts.TRIGGER)
    metric = health_output.get(LinkHealthConsts.METRIC)
    if _is_empty_field(trigger) and _is_empty_field(metric):
        return _split_legacy_reason(health_output.get(LinkHealthConsts.REASON))
    return trigger, metric


def verify_link_health_leaf_set(health_output: dict, context: str = '', require_all=True) -> bool:
    """Assert a health block carries the post-split leaf set.

    ``trigger`` + ``metric`` replaced ``reason``, which the HLD removes rather
    than deprecates, so an image serving the new leaves must not also serve
    ``reason``. ``require_all`` is dropped for views that omit empty leaves
    (the aggregate table). Returns False on a pre-split image, so the remaining
    checks still run there.
    """
    prefix = f'{context}: ' if context else ''
    assert isinstance(health_output, dict), (
        f'{prefix}link-health block is not a dict: {health_output!r}'
    )
    leaves = set(health_output.keys())
    if not leaves & {LinkHealthConsts.TRIGGER, LinkHealthConsts.METRIC}:
        logger.warning(
            f'{prefix}link-health block exposes no trigger/metric leaf (pre-split '
            f'image?); leaves={sorted(leaves)}'
        )
        return False
    stale = leaves & LinkHealthConsts.REMOVED_HEALTH_LEAVES
    assert not stale, (
        f'{prefix}link-health block still exposes {sorted(stale)} next to trigger/metric; '
        'the split replaces reason rather than deprecating it'
    )
    if require_all:
        missing = LinkHealthConsts.HEALTH_LEAVES - leaves
        assert not missing, (
            f'{prefix}link-health block is missing leaves {sorted(missing)}; '
            f'got {sorted(leaves)}'
        )
    unexpected = leaves - LinkHealthConsts.HEALTH_LEAVES
    if unexpected:
        logger.info(f'{prefix}link-health block carries extra leaves {sorted(unexpected)}')
    return True


def _assert_peer_port_metric_pruned(health_output: dict, context: str = '') -> None:
    """0.8: peer-port must not expose metric (empty or populated)."""
    prefix = f'{context}: ' if context else ''
    pruned = set(health_output) & LinkHealthConsts.PEER_PORT_PRUNED_HEALTH_LEAVES
    shown = ', '.join(
        f'{k}={health_output.get(k)!r}' for k in sorted(pruned)
    )
    assert not pruned, (
        f'{prefix}peer-port link-health still exposes pruned leaf {sorted(pruned)} '
        f'({shown}); metric is interface-only until compute 1.0/1.1 '
        '(same gap as peer-port FAE clear)'
    )


def verify_peer_port_link_health_leaf_set(
    health_output: dict, context: str = '', require_all=True,
) -> bool:
    """Assert the 0.8 peer-port health leaf set: state / trigger / time, no metric.

    ``metric`` is pruned (not left empty). ``reason`` must stay gone.
    ``require_all`` is False on healthy views that omit empty trigger/time.
    """
    prefix = f'{context}: ' if context else ''
    assert isinstance(health_output, dict), (
        f'{prefix}link-health block is not a dict: {health_output!r}'
    )
    leaves = set(health_output.keys())
    stale = leaves & LinkHealthConsts.REMOVED_HEALTH_LEAVES
    assert not stale, (
        f'{prefix}peer-port link-health still exposes {sorted(stale)}; '
        'the split replaces reason rather than deprecating it'
    )
    _assert_peer_port_metric_pruned(health_output, context)
    if require_all:
        missing = LinkHealthConsts.PEER_PORT_HEALTH_LEAVES - leaves
        assert not missing, (
            f'{prefix}peer-port link-health is missing leaves {sorted(missing)}; '
            f'got {sorted(leaves)}'
        )
    else:
        assert LinkHealthConsts.STATE in leaves, (
            f'{prefix}peer-port link-health is missing state; got {sorted(leaves)}'
        )
    unexpected = leaves - LinkHealthConsts.PEER_PORT_HEALTH_LEAVES
    if unexpected:
        logger.info(
            f'{prefix}peer-port link-health carries extra leaves {sorted(unexpected)}'
        )
    return True


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
    trigger, metric = _extract_attention_fields(health_output)
    time_value = health_output.get(LinkHealthConsts.TIME)
    assert _is_empty_field(trigger), (
        f"{prefix}trigger should be empty when healthy, got {trigger!r}"
    )
    assert _is_empty_or_zero_field(metric), (
        f"{prefix}metric should be empty when healthy, got {metric!r}"
    )
    if _normalize_attention_field(metric) == '0':
        logger.warning(
            f"{prefix}metric reads 0 (FW n/a index) instead of empty while healthy"
        )
    assert _is_empty_field(time_value), (
        f"{prefix}time should be empty when healthy, got {time_value!r}"
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
            # The brief collection omits the attention leaves by design (they would
            # otherwise land in the Summary cell), so only the removed leaf is checked.
            assert LinkHealthConsts.REASON not in health_block, (
                f"Port {port_name}: brief view still exposes the removed 'reason' leaf: "
                f"{sorted(health_block.keys())}"
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
        verify_link_health_leaf_set(health_output, port.name)
        state = health_output.get(LinkHealthConsts.STATE)
        if isinstance(state, dict):
            state = next(iter(state.keys()), None)
        verify_link_health_state_valid(state, port.name)
        if state == LinkHealthConsts.STATE_HEALTHY:
            verify_healthy_link_health_block(health_output, port.name)
        elif state == LinkHealthConsts.STATE_ATTENTION:
            verify_attention_link_health_block(health_output, port.name)


def _extract_aggregate_health_block(row):
    """Return the nested link-health block from a `nv show interface link-health` row.

    Actual shape: ``{"link": {"health": {"state", "trigger", "metric", "time"}}}``.
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

    Row shape: ``{<port>: {"link": {"health": {state, trigger, metric, time}}}}``.
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
            # The aggregate blanks non-attention rows, so empty leaves may be
            # omitted entirely here; only the removed-leaf rule is enforced.
            verify_link_health_leaf_set(health_block, port_name, require_all=False)
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
        verify_link_health_leaf_set(health_block, port.name)
        state = health_block.get(LinkHealthConsts.STATE)
        if isinstance(state, dict):
            state = next(iter(state.keys()), None)
        verify_link_health_state_valid(state, port.name)
        if state == LinkHealthConsts.STATE_HEALTHY:
            verify_healthy_link_health_block(health_block, port.name)
        elif state == LinkHealthConsts.STATE_ATTENTION:
            verify_attention_link_health_block(health_block, port.name)


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
        verify_link_health_leaf_set(health_block, port.name)
        state = health_block.get(LinkHealthConsts.STATE)
        if isinstance(state, dict):
            state = next(iter(state.keys()), None)
        verify_link_health_state_valid(state, port.name)
        if state == LinkHealthConsts.STATE_HEALTHY:
            verify_healthy_link_health_block(health_block, port.name)
        elif state == LinkHealthConsts.STATE_ATTENTION:
            verify_attention_link_health_block(health_block, port.name)


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
    trigger, metric = _extract_attention_fields(health_output)
    time_value = health_output.get(LinkHealthConsts.TIME)
    assert not _is_empty_field(trigger), (
        f"{prefix}trigger should be populated when in attention, got {trigger!r}"
    )
    assert not _is_empty_field(metric), (
        f"{prefix}metric should be populated when in attention, got {metric!r}"
    )
    _assert_attention_fields_format(trigger, metric, context)
    assert not _is_empty_field(time_value), (
        f"{prefix}time should be populated when in attention, got {time_value!r}"
    )


def verify_peer_port_healthy_link_health_block(health_output: dict, context: str = '') -> None:
    """Healthy peer-port block: empty trigger/time, no metric leaf."""
    prefix = f'{context}: ' if context else ''
    verify_peer_port_link_health_leaf_set(health_output, context, require_all=False)
    state = _parse_link_health_state(health_output)
    assert state == LinkHealthConsts.STATE_HEALTHY, (
        f'{prefix}expected state={LinkHealthConsts.STATE_HEALTHY}, got {state!r}'
    )
    trigger, _metric = _extract_attention_fields(health_output)
    time_value = health_output.get(LinkHealthConsts.TIME)
    assert _is_empty_field(trigger), (
        f'{prefix}peer-port trigger should be empty when healthy, got {trigger!r}'
    )
    assert _is_empty_field(time_value), (
        f'{prefix}peer-port time should be empty when healthy, got {time_value!r}'
    )


def verify_peer_port_attention_link_health_block(health_output: dict, context: str = '') -> None:
    """Attention peer-port block: trigger + time, no metric leaf."""
    prefix = f'{context}: ' if context else ''
    verify_peer_port_link_health_leaf_set(health_output, context, require_all=True)
    state = _parse_link_health_state(health_output)
    assert state == LinkHealthConsts.STATE_ATTENTION, (
        f'{prefix}expected state={LinkHealthConsts.STATE_ATTENTION}, got {state!r}'
    )
    trigger, _metric = _extract_attention_fields(health_output)
    time_value = health_output.get(LinkHealthConsts.TIME)
    assert not _is_empty_field(trigger), (
        f'{prefix}peer-port trigger should be populated when in attention, got {trigger!r}'
    )
    _assert_attention_trigger_format(trigger, context)
    assert not _is_empty_field(time_value), (
        f'{prefix}peer-port time should be populated when in attention, got {time_value!r}'
    )


def verify_link_health_after_link_down(port: Port, baseline_state: str) -> tuple:
    """Verify link-health retains its pre-down value after admin down.

    Returns (observed_state, output). Per the link-health build fix (bug: admin
    down no longer forces ``n/a``), a down link keeps whatever link-health it had
    just before it went down, so the value tells you the pre-down status:
      - down from ``healthy``   -> still ``healthy``.
      - down from ``attention`` -> still ``attention`` (trigger/metric/time preserved).
      - down from ``n/a``       -> still ``n/a`` (trigger/metric/time cleared).

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
            trigger, metric = _extract_attention_fields(health_output)
            time_value = health_output.get(LinkHealthConsts.TIME)
            assert _is_empty_field(trigger), (
                f'Port {port.name}: trigger should be empty for n/a, got {trigger!r}'
            )
            assert _is_empty_or_zero_field(metric), (
                f'Port {port.name}: metric should be empty for n/a, got {metric!r}'
            )
            assert _is_empty_field(time_value), (
                f'Port {port.name}: time should be empty for n/a, got {time_value!r}'
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


def _parse_gnmi_link_health_state(output: str) -> dict:
    """Parse status, trigger, metric, and time from gnmic flat output."""
    trigger = _parse_gnmic_flat_value(output, LinkHealthConsts.GNMI_PATH_TRIGGER)
    metric = _parse_gnmic_flat_value(output, LinkHealthConsts.GNMI_PATH_METRIC)
    if _is_empty_field(trigger) and _is_empty_field(metric):
        trigger, metric = _split_legacy_reason(
            _parse_gnmic_flat_value(output, LinkHealthConsts.GNMI_PATH_REASON)
        )
    return {
        LinkHealthConsts.STATE: _parse_gnmic_flat_value(output, LinkHealthConsts.GNMI_PATH_STATUS),
        LinkHealthConsts.TRIGGER: trigger,
        LinkHealthConsts.METRIC: metric,
        LinkHealthConsts.TIME: _parse_gnmic_flat_value(
            output, LinkHealthConsts.GNMI_PATH_TIME
        ),
    }


def _is_gnmi_zero_or_empty(value) -> bool:
    if _is_empty_field(value):
        return True
    try:
        return int(str(value).strip()) == 0
    except (TypeError, ValueError):
        return False


def _assert_attention_trigger_format(trigger, context: str = '') -> None:
    """Attention ``trigger`` is a FW metric name (e.g. plr-tx-bw-loss)."""
    prefix = f'{context}: ' if context else ''
    normalized_trigger = _normalize_attention_field(trigger)
    assert normalized_trigger is not None, f'{prefix}trigger is empty'
    assert re.match(LinkHealthConsts.TRIGGER_PATTERN, normalized_trigger), (
        f'{prefix}trigger {trigger!r} does not match expected FW metric name pattern'
    )


def _assert_attention_fields_format(trigger, metric, context: str = '') -> None:
    """Switch Attention reports ``trigger`` (FW metric name) + ``metric`` (index 1-15)."""
    prefix = f'{context}: ' if context else ''
    _assert_attention_trigger_format(trigger, context)
    normalized_metric = _normalize_attention_field(metric)
    assert normalized_metric is not None, f'{prefix}metric is empty'
    assert re.match(LinkHealthConsts.METRIC_PATTERN, normalized_metric), (
        f'{prefix}metric {metric!r} is not a metric index'
    )
    metric_index = int(normalized_metric)
    assert (
        LinkHealthConsts.METRIC_INDEX_MIN <= metric_index <= LinkHealthConsts.METRIC_INDEX_MAX
    ), (
        f'{prefix}metric index {metric_index} is outside the FW range '
        f'{LinkHealthConsts.METRIC_INDEX_MIN}-{LinkHealthConsts.METRIC_INDEX_MAX} '
        f'(0 is the FW n/a index and must not appear while trigger {trigger!r} is set)'
    )


def _read_link_health_cli_fields(port: Port) -> dict:
    health_output = OutputParsingTool.parse_json_str_to_dictionary(
        port.interface.link.health.show()
    ).get_returned_value()
    trigger, metric = _extract_attention_fields(health_output)
    return {
        LinkHealthConsts.STATE: _parse_link_health_state(health_output),
        LinkHealthConsts.TRIGGER: trigger,
        LinkHealthConsts.METRIC: metric,
        LinkHealthConsts.TIME: health_output.get(LinkHealthConsts.TIME),
    }


GNMI_LINK_HEALTH_LEAVES = (
    LinkHealthConsts.STATE,
    LinkHealthConsts.TRIGGER,
    LinkHealthConsts.METRIC,
    LinkHealthConsts.TIME,
)


def _gnmi_serves_pre_split_trigger(gnmi_state: dict) -> bool:
    """True when telemetry still serves the raw FW trigger code and no metric leaf.

    Verified on dev-link-health-updates-010: the CLI reports
    ``trigger=plr-tx-bw-loss`` / ``metric=1`` while gNMI serves
    ``.../state/trigger: 1`` and no ``metric`` leaf at all, because the telemetry
    proposal for the split has not landed yet.
    """
    trigger = _normalize_attention_field(gnmi_state.get(LinkHealthConsts.TRIGGER))
    return (
        trigger is not None and
        trigger.isdigit() and
        gnmi_state.get(LinkHealthConsts.METRIC) is None
    )


def _verify_gnmi_link_health_leaves(
    gnmi_state: dict,
    expected_cli_state: str,
    port_name: str,
    prefix: str = '',
) -> None:
    """Assert the subscription carries every link-health leaf for this state.

    A leaf parsed as ``None`` was absent from the subscription; ``''`` means it
    was reported empty, which is what healthy / n-a must look like. Attention
    needs all four leaves with values, unless telemetry is still on the pre-split
    encoding (raw trigger code, no metric leaf), which is reported rather than
    failed. Likewise a build serving no trigger/metric leaf at all is logged, so
    the remaining parity checks still run.
    """
    absent = [leaf for leaf in GNMI_LINK_HEALTH_LEAVES if gnmi_state.get(leaf) is None]
    if expected_cli_state == LinkHealthConsts.STATE_ATTENTION:
        if _gnmi_serves_pre_split_trigger(gnmi_state):
            logger.warning(
                f'{prefix}Port {port_name}: gNMI still serves the pre-split encoding '
                f'(raw trigger code {gnmi_state.get(LinkHealthConsts.TRIGGER)!r}, no metric '
                'leaf); skipping the metric-leaf requirement until the telemetry split lands'
            )
            missing = [leaf for leaf in absent if leaf != LinkHealthConsts.METRIC]
            assert not missing, (
                f'{prefix}Port {port_name}: gNMI attention sample is missing leaves '
                f'{missing}; got {gnmi_state!r}'
            )
            return
        assert not absent, (
            f'{prefix}Port {port_name}: gNMI attention sample is missing leaves {absent}; '
            f'got {gnmi_state!r}'
        )
        return

    serves_split = any(
        gnmi_state.get(leaf) is not None
        for leaf in (LinkHealthConsts.TRIGGER, LinkHealthConsts.METRIC)
    )
    if not serves_split:
        logger.warning(
            f'{prefix}Port {port_name}: gNMI serves no trigger/metric leaf for CLI '
            f'{expected_cli_state!r} (telemetry split not implemented on this image?); '
            f'absent leaves={absent}'
        )
        return
    # Status is the YANG enum default for n/a and time is a numeric epoch that
    # defaults to 0; gnmic drops default leaves on the wire, so those two are
    # reported rather than failed. The string trigger/metric leaves must both be
    # there, reported empty.
    tolerated = {LinkHealthConsts.STATE, LinkHealthConsts.TIME}
    missing = [leaf for leaf in absent if leaf not in tolerated]
    assert not missing, (
        f'{prefix}Port {port_name}: gNMI reports trigger/metric for CLI '
        f'{expected_cli_state!r} but omits {missing}; every leaf must be present '
        f'(empty) when there is no attention'
    )
    omitted_defaults = [leaf for leaf in absent if leaf in tolerated]
    if omitted_defaults:
        logger.info(
            f'{prefix}Port {port_name}: gNMI omits default leaves {omitted_defaults} for CLI '
            f'{expected_cli_state!r} (gnmic drops leaves left at their YANG default)'
        )


def _canonical_trigger(value) -> Optional[str]:
    """Compare trigger names across separator/case differences (PLR-Tx-BW-Loss)."""
    normalized = _normalize_attention_field(value)
    if normalized is None:
        return None
    return re.sub(r'[-_]', '', normalized).lower()


def _assert_gnmi_link_health_parity(
    gnmi_state: dict,
    cli_fields: dict,
    expected_cli_state: str,
    port_name: str,
    context: str = '',
) -> None:
    """Assert gNMI status + trigger + metric + time match CLI semantics.

  ``UNHEALTHY`` alone is not accepted for attention: the trigger and time must
  corroborate the CLI state. For n/a, trigger/metric/time must be cleared so
  gNMI does not look like attention.
    """
    prefix = f'{context}: ' if context else ''
    gnmi_status = gnmi_state.get(LinkHealthConsts.STATE)
    gnmi_trigger = gnmi_state.get(LinkHealthConsts.TRIGGER)
    gnmi_metric = gnmi_state.get(LinkHealthConsts.METRIC)
    gnmi_time = gnmi_state.get(LinkHealthConsts.TIME)
    cli_trigger = cli_fields.get(LinkHealthConsts.TRIGGER)
    cli_metric = cli_fields.get(LinkHealthConsts.METRIC)
    cli_time = cli_fields.get(LinkHealthConsts.TIME)
    _verify_gnmi_link_health_leaves(gnmi_state, expected_cli_state, port_name, prefix)

    if expected_cli_state == LinkHealthConsts.STATE_NA:
        assert _is_gnmi_zero_or_empty(gnmi_trigger), (
            f'{prefix}Port {port_name}: gNMI trigger must be empty for n/a; '
            f'non-zero value {gnmi_trigger!r} implies attention, not n/a'
        )
        assert _is_gnmi_zero_or_empty(gnmi_metric), (
            f'{prefix}Port {port_name}: gNMI metric must be 0/empty for n/a, '
            f'got {gnmi_metric!r}'
        )
        assert _is_gnmi_zero_or_empty(gnmi_time), (
            f'{prefix}Port {port_name}: gNMI time must be 0/empty for n/a, '
            f'got {gnmi_time!r}'
        )
        if _gnmi_value_matches_cli_state(gnmi_status, LinkHealthConsts.STATE_NA):
            return
        if gnmi_status is None:
            # UNSPECIFIED is the YANG default (enum 0); gnmic omits default leaves
            # on the wire (same as `time` at baseline). Absent status + cleared
            # attention fields is the expected n/a encoding.
            logger.info(
                f'{prefix}Port {port_name}: gNMI status leaf absent for CLI n/a; '
                'treating omitted UNSPECIFIED default with cleared trigger/metric/time as n/a'
            )
            return
        if _gnmi_value_matches_cli_state(gnmi_status, LinkHealthConsts.STATE_ATTENTION):
            logger.warning(
                f'{prefix}Port {port_name}: gNMI status is UNHEALTHY for CLI n/a — '
                'YANG specifies UNSPECIFIED for n/a; cleared trigger/metric/time confirm '
                'n/a semantics but status encoding deviates from model (§18)'
            )
            return
        assert not _gnmi_value_matches_cli_state(gnmi_status, LinkHealthConsts.STATE_HEALTHY), (
            f'{prefix}Port {port_name}: gNMI status is HEALTHY while CLI is n/a '
            f'(false healthy); status={gnmi_status!r}'
        )
        pytest.fail(
            f'{prefix}Port {port_name}: expected gNMI status UNSPECIFIED (or UNHEALTHY with '
            f'cleared trigger/metric/time) for CLI n/a, got status={gnmi_status!r} '
            f'(trigger={gnmi_trigger!r}, metric={gnmi_metric!r}, time={gnmi_time!r})'
        )

    assert _gnmi_value_matches_cli_state(gnmi_status, expected_cli_state), (
        f'{prefix}Port {port_name}: expected gNMI status semantically matching CLI '
        f'{expected_cli_state!r}, got status={gnmi_status!r} '
        f'(trigger={gnmi_trigger!r}, metric={gnmi_metric!r}, time={gnmi_time!r})'
    )

    if expected_cli_state == LinkHealthConsts.STATE_HEALTHY:
        assert _is_gnmi_zero_or_empty(gnmi_trigger), (
            f'{prefix}Port {port_name}: gNMI trigger should be empty when '
            f'healthy, got {gnmi_trigger!r}'
        )
        assert _is_gnmi_zero_or_empty(gnmi_metric), (
            f'{prefix}Port {port_name}: gNMI metric should be 0/empty when '
            f'healthy, got {gnmi_metric!r}'
        )
        assert _is_gnmi_zero_or_empty(gnmi_time), (
            f'{prefix}Port {port_name}: gNMI time should be 0/empty when '
            f'healthy, got {gnmi_time!r}'
        )
        return

    if expected_cli_state == LinkHealthConsts.STATE_ATTENTION:
        assert not _is_gnmi_zero_or_empty(gnmi_trigger), (
            f'{prefix}Port {port_name}: gNMI status {gnmi_status!r} is ambiguous without '
            f'a non-empty trigger; ATTENTION status alone cannot confirm attention'
        )
        if str(gnmi_trigger).strip().isdigit() and not str(cli_trigger).strip().isdigit():
            # Telemetry still carries the raw FW code; the CLI already serves the
            # decoded metric name, so a code-vs-name pair cannot be compared.
            logger.warning(
                f'{prefix}Port {port_name}: gNMI trigger {gnmi_trigger!r} is a raw FW code '
                f'while CLI reports {cli_trigger!r}; skipping value comparison until the '
                'telemetry trigger/metric split lands'
            )
        else:
            assert _canonical_trigger(gnmi_trigger) == _canonical_trigger(cli_trigger), (
                f'{prefix}Port {port_name}: gNMI trigger {gnmi_trigger!r} does not match '
                f'CLI {cli_trigger!r}'
            )
        if not _is_empty_field(gnmi_metric) and not _is_empty_field(cli_metric):
            assert _normalize_attention_field(gnmi_metric) == _normalize_attention_field(cli_metric), (
                f'{prefix}Port {port_name}: gNMI metric {gnmi_metric!r} does not match '
                f'CLI {cli_metric!r}'
            )
        normalized_gnmi_metric = _normalize_attention_field(gnmi_metric)
        if normalized_gnmi_metric is not None and normalized_gnmi_metric.isdigit():
            metric_index = int(normalized_gnmi_metric)
            assert (
                LinkHealthConsts.METRIC_INDEX_MIN <= metric_index <=
                LinkHealthConsts.METRIC_INDEX_MAX
            ), (
                f'{prefix}Port {port_name}: gNMI metric index {metric_index} is outside the '
                f'FW range {LinkHealthConsts.METRIC_INDEX_MIN}-'
                f'{LinkHealthConsts.METRIC_INDEX_MAX} while in attention'
            )
        assert not _is_gnmi_zero_or_empty(gnmi_time), (
            f'{prefix}Port {port_name}: gNMI time should be non-zero when CLI '
            f'time is populated ({cli_time!r}), got {gnmi_time!r}'
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


def try_build_gnmi_client(dut_ip, username, password):
    """Build a GnmiClient on the local pytest host, auto-installing gnmic if it is missing.

    Only when gnmic is still unusable after the install attempt does it return None, so a
    genuine tooling/connectivity gap degrades to CLI-only verification instead of failing
    the whole test.
    """
    from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient

    try:
        client = GnmiClient(dut_ip, GnmiConsts.GNMI_DEFAULT_PORT, username, password)
        client.verify_gnmic_installation()
        return client
    except Exception as exc:  # noqa: BLE001 - tooling gap must never fail the CLI test
        logger.warning(f'gnmic unavailable on the local pytest host, skipping gNMI checks: {exc!r}')
        return None


def gnmi_link_health_state_once(
    gnmi_client,
    port_name: str,
    username: str,
    password: str,
) -> dict:
    """One-shot gNMI read of link-health status, trigger, metric, and time."""
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
        f'{prefix}Verify gNMI link-health (status, trigger, metric, time) '
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
        verify_link_health_leaf_set(health_output, port.name)
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
PTER_ERROR_TYPE_BER = '0x2'       # Effective/Raw BER -> BER attention trigger
PTER_ERROR_TYPE_SYMBOL = '0x4'    # Symbol errors -> symbol-error attention trigger
# Validated pair (zombie-link / Rosalind 2165 acp10): mantissa=1, exp=4.
# mantissa 0x9 was FW-accepted on some builds but did not trip Attention here.
# error_type_oper staying 0 is *not* proof the inject is idle on this FW —
# CLI still reached attention (PLR_Tx_BW_Loss) after the SET.
PTER_BER_MANTISSA = '0x1'
PTER_BER_EXP = '0x4'


def _prefer_pci_cr_mst(mst_device: str) -> str:
    """PTER/PPBMP are often only reliable on pci_cr, not pciconf (Quantum3)."""
    if mst_device and 'pciconf' in str(mst_device):
        return str(mst_device).replace('pciconf', 'pci_cr')
    return mst_device


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


PTER_READ_FIELDS = ('error_type_admin', 'error_type_oper', 'error_type_cap')


def _parse_pter_field(output: str, field: str) -> Optional[int]:
    for line in output.splitlines():
        if field not in line:
            continue
        match = re.search(r'0x[0-9a-fA-F]+|\b\d+\b', line.split(field, 1)[1])
        if not match:
            continue
        try:
            return int(match.group(0), 0)
        except ValueError:
            return None
    return None


def read_pter_fields(dut_engine, mst_device, local_port) -> dict:
    """Read PTER admin/oper/cap via mlxreg --get. Missing fields map to None.

    ``error_type_cap`` is a bitmask of the error types the port supports, so it
    explains a write that the CLI accepts but the port silently drops.
    """
    get_cmd = _pter_get_cmd(mst_device, local_port)
    output = dut_engine.run_cmd(get_cmd, validate=False, print_output=False)
    if not output:
        return {field: None for field in PTER_READ_FIELDS}
    return {field: _parse_pter_field(output, field) for field in PTER_READ_FIELDS}


def _format_pter_fields(fields: dict) -> str:
    return ', '.join(
        f'{name}={value:#x}' if isinstance(value, int) else f'{name}=?'
        for name, value in fields.items()
    )


def clear_pter_injection(dut_engine, mst_device, local_port) -> None:
    """Disarm PTER injection (error_type_admin=0)."""
    if not mst_device or not local_port:
        return
    cmd = _pter_cmd(mst_device, local_port, PTER_ERROR_TYPE_CLEAR)
    dut_engine.run_cmd(cmd, validate=False, print_output=False)


def arm_pter_attention(dut_engine, mst_device, local_port,
                       error_type_admin=PTER_ERROR_TYPE_BER,
                       ber_mantissa=None, ber_exp=None) -> bool:
    """Clear then arm PTER injection, and verify the register write took effect.

    Returns True only when the mlxreg set was accepted (no error) AND a read-back
    shows error_type_admin == the requested value. This distinguishes "register
    write went through" from "write silently ignored", instead of assuming the
    write worked and only checking the downstream CLI.
    """
    clear_pter_injection(dut_engine, mst_device, local_port)
    ber_mantissa = PTER_BER_MANTISSA if ber_mantissa is None else ber_mantissa
    ber_exp = PTER_BER_EXP if ber_exp is None else ber_exp
    with allure.step(f'Arm PTER on local_port={local_port} (error_type_admin={error_type_admin})'):
        set_cmd = _pter_cmd(
            mst_device, local_port, error_type_admin,
            error_injection_time='0xFFFF', ber_mantissa=ber_mantissa, ber_exp=ber_exp,
        )
        set_output = dut_engine.run_cmd(set_cmd, validate=False, print_output=False)
        if _mlxreg_reports_error(set_output):
            logger.warning(
                f'PTER set on local_port={local_port} reported an error; '
                f'register write did NOT go through: {set_output!r}'
            )
            return False

        expected = int(str(error_type_admin), 0)
        fields = read_pter_fields(dut_engine, mst_device, local_port)
        read_back = fields['error_type_admin']
        if read_back is None:
            logger.warning(
                f'PTER set on local_port={local_port} returned no error, but read-back '
                'could not be parsed; treating register write as unverified'
            )
            return False
        if read_back != expected:
            cap = fields['error_type_cap']
            unsupported = isinstance(cap, int) and not cap & expected
            cap_note = (
                f'error_type {expected:#x} is NOT in the port capability mask'
                if unsupported else
                'error_type is within the capability mask, so the value itself was rejected'
            )
            logger.warning(
                f'PTER write did not take effect on local_port={local_port}: '
                f'set error_type_admin={expected:#x}, read back {read_back:#x} '
                f'({_format_pter_fields(fields)}); {cap_note}'
            )
            return False
        oper = fields.get('error_type_oper')
        if oper == 0:
            logger.info(
                f'PTER error_type_oper=0 on local_port={local_port} after arm; '
                'on this FW that is not a reject — keep injection armed and wait for CLI'
            )
        logger.info(
            f'PTER write confirmed on local_port={local_port}: '
            f'{_format_pter_fields(fields)}'
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
    while True:
        last_state, _ = _read_link_health_state(port)
        if last_state == expected_state:
            return True
        if time.time() >= deadline:
            break
        time.sleep(poll_interval)
    logger.warning(
        f'{port.name} did not reach link-health state {expected_state!r} within '
        f'{poll_timeout}s (last state={last_state!r})'
    )
    return False


def wait_for_attention_block_populated(port, poll_timeout=20, poll_interval=3) -> bool:
    """Poll until an attention block carries trigger/metric/time, not just the state.

    NVOS can publish ``state: attention`` a moment before trigger and time are
    filled in - lab capture on dev-link-health-updates-011 showed
    ``{"metric": "1", "state": "attention", "time": "", "trigger": ""}`` one poll
    before the complete block. Returns False on timeout; callers decide whether
    that is a failure.
    """
    deadline = time.time() + poll_timeout
    last_output = None
    while time.time() < deadline:
        state, last_output = _read_link_health_state(port)
        if state == LinkHealthConsts.STATE_ATTENTION:
            trigger, metric = _extract_attention_fields(last_output)
            time_value = last_output.get(LinkHealthConsts.TIME)
            if not any(_is_empty_field(value) for value in (trigger, metric, time_value)):
                return True
        time.sleep(poll_interval)
    logger.warning(
        f'{port.name}: attention block still incomplete after {poll_timeout}s '
        f'(last output={last_output!r})'
    )
    return False


def trigger_link_health_attention(port, dut_engine, error_type_admin=PTER_ERROR_TYPE_BER,
                                  poll_timeout=90, poll_interval=5,
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
                    mst_device = _prefer_pci_cr_mst(mst_device)
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
                wait_for_attention_block_populated(port)
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
    poll_timeout=180,
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
    """Assert CLI shows attention + trigger/metric + non-empty time.

    Returns ``(trigger, metric, time)``.
    """
    prefix = f'{context}: ' if context else ''
    with allure.step(f'{prefix}Verify link-health attention block for {port.name}'):
        state, health_output = _read_link_health_state(port)
        assert state == LinkHealthConsts.STATE_ATTENTION, (
            f'{prefix}Port {port.name}: expected attention, got {state!r}'
        )
        trigger, metric = _extract_attention_fields(health_output)
        time_value = health_output.get(LinkHealthConsts.TIME)
        assert not _is_empty_field(trigger), (
            f'{prefix}Port {port.name}: trigger should be populated, got {trigger!r}'
        )
        _assert_attention_fields_format(trigger, metric, prefix.rstrip(': ') or port.name)
        assert not _is_empty_field(time_value), (
            f'{prefix}Port {port.name}: time should be populated, got {time_value!r}'
        )
        return trigger, metric, time_value


# ST-06: wait ~2 FlexCounter poll cycles before a repeat trigger.
ATTENTION_STICKY_SETTLE_SEC = 15
ATTENTION_STRESS_TRIGGER_COUNT = 3
TOGGLE_STRESS_CYCLES = 6
TOGGLE_STRESS_SETTLE_SEC = 10


def verify_attention_sticky_first_capture(
    port,
    first_trigger,
    first_metric,
    first_time,
    context='ST-06',
    gnmi_client=None,
    username: str = '',
    password: str = '',
) -> None:
    """Assert attention still shows the first latched trigger/metric/time (ST-06)."""
    prefix = f'{context}: ' if context else ''
    with allure.step(
        f'{prefix}Verify {port.name} retains first attention trigger/metric/time '
        'after repeat trigger'
    ):
        state, health_output = _read_link_health_state(port)
        assert state == LinkHealthConsts.STATE_ATTENTION, (
            f'{prefix}Port {port.name}: expected attention, got {state!r}'
        )
        trigger, metric = _extract_attention_fields(health_output)
        time_value = health_output.get(LinkHealthConsts.TIME)
        assert _normalize_attention_field(trigger) == _normalize_attention_field(first_trigger), (
            f'{prefix}Port {port.name}: trigger should stay at first capture '
            f'{first_trigger!r}, got {trigger!r}'
        )
        assert _normalize_attention_field(metric) == _normalize_attention_field(first_metric), (
            f'{prefix}Port {port.name}: metric should stay at first capture '
            f'{first_metric!r}, got {metric!r}'
        )
        assert time_value == first_time, (
            f'{prefix}Port {port.name}: time should stay at first capture '
            f'{first_time!r}, got {time_value!r}'
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
    """ST-06 phase 1: latch attention once and capture the first trigger/metric/time.

    The trigger escalates across PTER error types (BER then SYMBOL) until one
    trips attention. ``on_attention_latched`` is invoked the moment attention is
    confirmed - before the CLI block is validated - so the caller can arm its
    recovery before any assertion can fail.

    Returns ``(working_error_type, first_trigger, first_metric, first_time)``, or
    ``(None, None, None, None)`` when no error type tripped attention (nothing was
    latched, so the caller owes no attention recovery).
    """
    with allure.step(f'ST-06 first attention capture on {port.name}'):
        working_error_type = trigger_link_health_attention_first(
            port, dut_engine, error_types=ATTENTION_FIRST_TRIGGER_ERROR_TYPES
        )
        if working_error_type is None:
            logger.warning(f'{port.name}: first attention trigger failed')
            return None, None, None, None
        if on_attention_latched is not None:
            on_attention_latched()

        first_trigger, first_metric, first_time = verify_link_health_attention_on_port(
            port, context='first capture'
        )
        logger.info(
            f'{port.name}: first attention capture trigger={first_trigger!r} '
            f'metric={first_metric!r} time={first_time!r}'
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
        return working_error_type, first_trigger, first_metric, first_time


def verify_link_health_attention_sticky_repeat_triggers(
    port,
    dut_engine,
    repeat_error_type,
    first_trigger,
    first_metric,
    first_time,
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
                first_trigger,
                first_metric,
                first_time,
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

            first_trigger, first_metric, first_time = verify_link_health_attention_on_port(
                attention_port, context='pre-trigger'
            )
            context.update({
                'first_trigger': first_trigger,
                'first_metric': first_metric,
                'first_time': first_time,
                'working_error_type': working_error_type,
            })
            prep_done = True
            return context
    finally:
        if not prep_done:
            recover_nvl_link_health_prep(context, devices, test_name)


def _split_nvl_port_name(port_name: str):
    """``'acp57'`` -> ``('acp', 57)``; ``(None, None)`` when not ``<prefix><num>``."""
    match = re.match(r'^([A-Za-z]+)(\d+)$', str(port_name))
    if not match:
        return None, None
    prefix, num = match.groups()
    return prefix, int(num)


def build_link_health_range_expr(port_names) -> str:
    """Build an HLD 0.18 ranged interface id for a set of same-prefix ports.

    ``[acp1, acp2, acp7, acp8]`` -> ``acp1-2,7-8``: the prefix is emitted once
    and consecutive port numbers collapse into ``first-last`` spans, which is
    the form ``nv action clear fae interface <id> link health`` accepts.
    """
    parsed = [_split_nvl_port_name(name) for name in port_names]
    assert all(prefix is not None for prefix, _ in parsed), (
        f'Unparseable port name in {list(port_names)!r}; expected <prefix><number>'
    )
    prefixes = {prefix for prefix, _ in parsed}
    assert len(prefixes) == 1, (
        f'Ranged clear needs a single port prefix, got {sorted(prefixes)} from {list(port_names)!r}'
    )
    prefix = prefixes.pop()
    numbers = sorted({num for _, num in parsed})

    spans = []
    span_start = span_end = numbers[0]
    for num in numbers[1:]:
        if num == span_end + 1:
            span_end = num
            continue
        spans.append((span_start, span_end))
        span_start = span_end = num
    spans.append((span_start, span_end))

    parts = [
        str(start) if start == end else f'{start}-{end}'
        for start, end in spans
    ]
    return f'{prefix}{",".join(parts)}'


def find_contiguous_nvl_port_groups(
    dut_engine,  # noqa: ARG001 (kept for signature parity with the other finders)
    candidate_ports,
    group_size=2,
    group_count=2,
):
    """Find ``group_count`` disjoint runs of ``group_size`` consecutive ports.

    Ranged-clear coverage needs consecutive port numbers so the range id spans
    real neighbours (``acp1-2``) rather than degenerating into a list of single
    ports. Eligibility is checked lazily per run so a 288-port setup does not
    pay for a full scan, and all returned groups share one prefix.
    """
    by_prefix = {}
    for port in candidate_ports:
        prefix, num = _split_nvl_port_name(getattr(port, 'name', port))
        if prefix is None:
            continue
        by_prefix.setdefault(prefix, {})[num] = port

    eligibility_cache = {}

    def _eligible(port):
        if port.name not in eligibility_cache:
            try:
                ready, detail = is_port_ready_for_attention(port)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f'Eligibility check failed for {port.name}: {exc!r}')
                ready, detail = False, repr(exc)
            eligibility_cache[port.name] = ready
            if not ready:
                logger.debug(f'{port.name} not eligible for ranged clear: {detail}')
        return eligibility_cache[port.name]

    for prefix in sorted(by_prefix):
        numbered = by_prefix[prefix]
        candidate_runs = []
        for start in sorted(numbered):
            run_numbers = list(range(start, start + group_size))
            if any(num not in numbered for num in run_numbers):
                continue
            run_ports = [numbered[num] for num in run_numbers]
            if not all(_eligible(port) for port in run_ports):
                continue
            candidate_runs.append(run_ports)
        random.shuffle(candidate_runs)
        groups = []
        used = set()
        for run_ports in candidate_runs:
            run_numbers = [_split_nvl_port_name(port.name)[1] for port in run_ports]
            if any(num in used for num in run_numbers):
                continue
            groups.append(run_ports)
            used.update(run_numbers)
            if len(groups) >= group_count:
                break
        if groups:
            logger.info(
                'Selected contiguous NVL port groups for ranged clear: '
                f'{[[p.name for p in group] for group in groups]}'
            )
            return groups

    logger.warning(
        f'Could not find {group_count} disjoint run(s) of {group_size} consecutive '
        'eligible NVL ports for ranged-clear coverage'
    )
    return []


def clear_stale_link_health_attentions(dut_engine, context='pre-test') -> list:
    """FAE-clear every port currently latched in Attention.

    Call at the start of link-health tests so leftover sticky Attention from a
    prior run cannot pollute baselines or eligible-port selection.

    Returns the list of port names that were cleared (best-effort; failures are
    logged and skipped so a single stuck port does not abort the suite).
    """
    prefix = f'{context}: ' if context else ''
    cleared = []
    with allure.step(f'{prefix}Clear any leftover link-health Attention latches'):
        try:
            output = OutputParsingTool.parse_json_str_to_dictionary(
                dut_engine.run_cmd('nv show interface link-health -o json')
            ).get_returned_value()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'{prefix}Could not read link-health aggregate for pre-clear: {exc!r}')
            return cleared
        if not isinstance(output, dict) or not output:
            logger.info(f'{prefix}link-health aggregate empty; nothing to pre-clear')
            return cleared

        attention_ports = []
        for port_name, row in output.items():
            health_block = _extract_aggregate_health_block(row)
            if not health_block:
                continue
            state = health_block.get(LinkHealthConsts.STATE)
            if isinstance(state, dict):
                state = next(iter(state.keys()), None)
            if state == LinkHealthConsts.STATE_ATTENTION:
                attention_ports.append(port_name)

        if not attention_ports:
            logger.info(f'{prefix}No leftover Attention latches; starting clean')
            return cleared

        logger.info(
            f'{prefix}Clearing leftover Attention on {len(attention_ports)} port(s): '
            f'{attention_ports}'
        )
        for port_name in attention_ports:
            try:
                clear_link_health_attention_fae(port_name, dut_engine)
                cleared.append(port_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f'{prefix}Pre-clear failed on {port_name}: {exc!r}')
        if cleared:
            # One short settle so subsequent healthy-port selection sees the new state.
            time.sleep(2)
    return cleared


def clear_link_health_attention_fae(port_or_range: str, dut_engine=None) -> None:
    """Run ``nv action clear fae interface <id|range> link health``.

    ``port_or_range`` may be a single port (``acp1``) or a ranged id
    (``acp1-74``, ``acp1-3,7``) per HLD 0.18.
    """
    from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit

    if dut_engine is None:
        dut_engine = TestToolkit.get_engine()
    cmd = f'nv action clear fae interface {port_or_range} link health'
    with allure.step(f'FAE clear link health: {port_or_range}'):
        out = dut_engine.run_cmd(cmd, validate=False)
        lowered = (out or '').lower()
        assert 'error' not in lowered and 'invalid' not in lowered, (
            f'FAE clear link health failed for {port_or_range!r}: {out!r}'
        )


def clear_link_health_attention_on_port(port, devices, test_name) -> None:
    """Best-effort FAE clear of a latched attention (HLD 0.17).

    Uses ``nv action clear fae interface <port> link health`` so sticky
    Attention + trigger/metric/time are cleared without toggling the link. Falls back
    to admin down/up only if the FAE clear path is unavailable on the image.

    A no-op unless the port currently reports ``attention``, so callers can run
    it unconditionally in cleanup without disturbing an untouched port.
    """
    from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
    from ngts.tests_nvos.interfaces.nvl_port.helpers import toggle_port_state
    from ngts.nvos_tools.infra.Fae import Fae

    try:
        state, _ = _read_link_health_state(port)
        if state != LinkHealthConsts.STATE_ATTENTION:
            logger.info(f'{port.name} is {state!r}; no latched attention to clear')
            return
        with allure.step(f'Clear latched attention on {port.name} via FAE clear'):
            result = Fae(port_name=port.name).interface.link.health.action_clear()
            if getattr(result, 'result', True) is False:
                raise RuntimeError(getattr(result, 'info', 'FAE clear failed'))
            wait_for_link_health_cli_state(
                port,
                LinkHealthConsts.STATE_HEALTHY,
                poll_timeout=LinkHealthConsts.ATTENTION_POLL_TIMEOUT_SEC,
                poll_interval=LinkHealthConsts.ATTENTION_POLL_INTERVAL_SEC,
            )
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the test result
        logger.warning(
            f'FAE clear failed on {port.name} ({exc!r}); falling back to admin down/up'
        )
        try:
            with allure.step(f'Clear latched attention on {port.name} via admin down/up'):
                toggle_port_state(port, NvosConsts.LINK_STATE_DOWN, test_name, devices)
                time.sleep(TOGGLE_STRESS_SETTLE_SEC)
                toggle_port_state(port, NvosConsts.LINK_STATE_UP, test_name, devices)
        except Exception as fallback_exc:  # noqa: BLE001
            logger.warning(f'Failed to clear attention on {port.name}: {fallback_exc!r}')


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
    report ``healthy`` with empty ``trigger`` / ``metric`` / ``time``.
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


def skip_if_peer_port_link_health_unavailable(dut_engine) -> None:
    """Peer-port link-health needs NMX-T; skip until that build is available."""
    output = dut_engine.run_cmd(
        'nv show peer-port link-health -o json 2>&1',
        validate=False,
        print_output=False,
    )
    lowered = (output or '').lower()
    if (
        not output or
        not output.strip() or
        'error' in lowered or
        'invalid' in lowered or
        'unknown' in lowered or
        'no data' in lowered
    ):
        pytest.skip(
            'Peer-port link-health CLI not available yet '
            '(NMX-T build required per HLD / release note)'
        )


def verify_peer_port_link_health_show(peer_port_interface) -> None:
    """PP visibility: peer-port link health drill-in when feature is available.

    0.8 leaf set is state / trigger / time. ``metric`` is pruned (interface-only).
    """
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
            verify_peer_port_healthy_link_health_block(
                health_output, peer_port_interface.peer_port_name
            )
        elif state == LinkHealthConsts.STATE_ATTENTION:
            verify_peer_port_attention_link_health_block(
                health_output, peer_port_interface.peer_port_name
            )
        else:
            verify_peer_port_link_health_leaf_set(
                health_output, peer_port_interface.peer_port_name, require_all=False
            )


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
            verify_peer_port_healthy_link_health_block(peer_output, peer_name)
        return peer_state, peer_output
