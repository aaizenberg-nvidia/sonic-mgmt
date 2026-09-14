"""CLI orchestration for the NVOS OTEL test-plan Section 4 configuration tests.

Thin wrappers over the ``System().telemetry`` component model: resolve the node a
parametrized transport / metric family maps to, stage a knob, and read the
``applied`` datastore back so a test body reads as the plan step it implements.

Staging helpers take ``apply=True`` to commit the candidate configuration, so the
commit is dispatched by the component model to whichever API the test runs under.

Owned by the NVOS test-plan suite (``test_otel_nvos_cli_and_functional``); the
ported Cumulus modules keep their own helpers under ``otel/cumulus/``.
"""

from __future__ import annotations

from typing import Any, Dict

from ngts.nvos_constants.constants_nvos import ConfState, TelemetryConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.system.Telemetry import (
    TelemetryOtlpDestination,
    TelemetryStatsGroup,
)

_ENABLED = TelemetryConsts.State.ENABLED.value


# --- Node resolution ---------------------------------------------------------


def transport_node(system: System, transport: str) -> BaseComponent:
    """Return the ``export/otlp/<transport>`` node for ``grpc`` or ``http``."""
    otlp = system.telemetry.export.otlp
    nodes = {TelemetryConsts.GRPC: otlp.grpc, TelemetryConsts.HTTP: otlp.http}
    if transport not in nodes:
        raise ValueError(
            f"Unknown OTLP transport {transport!r}; expected one of {sorted(nodes)}"
        )
    return nodes[transport]


def family_node(parent: BaseComponent, family: str) -> BaseComponent:
    """Return the metric-family node under ``parent``.

    ``parent`` is either ``system.telemetry`` or a ``stats-group/<id>`` node -
    both host the same four stats subtrees.
    """
    nodes = {
        TelemetryConsts.INTERFACE_STATS: parent.interface_stats,
        TelemetryConsts.PEER_PORT_STATS: parent.peer_port_stats,
        TelemetryConsts.IB_ROUTER_STATS: parent.ib_router_stats,
        TelemetryConsts.PLATFORM_STATS: parent.platform_stats,
    }
    if family not in nodes:
        raise ValueError(
            f"Unknown metric family {family!r}; expected one of {sorted(nodes)}"
        )
    return nodes[family]


def sample_interval_node(family: str, node: BaseComponent) -> BaseComponent:
    """Return the node that hosts ``sample-interval`` for ``family``.

    platform-stats has no top-level ``sample-interval``; it lives under ``export``.
    """
    if family == TelemetryConsts.PLATFORM_STATS:
        return node.export
    return node


def stats_group_node(system: System, group_id: str) -> TelemetryStatsGroup:
    """Return the ``stats-group/<id>`` node with its four stats subtrees attached."""
    return TelemetryStatsGroup(system.telemetry.stats_group, group_id)


def destination_node(
    system: System, transport: str, dest_id: str
) -> TelemetryOtlpDestination:
    """Return the ``export/otlp/<transport>/destination/<id>`` node."""
    return TelemetryOtlpDestination(transport_node(system, transport).destination, dest_id)


def interface_label_node(interface: str, label_id: str) -> BaseComponent:
    """Return the ``interface/<id>/telemetry/label/<label-id>`` node."""
    return BaseComponent(
        Interface(None, port_name=interface), path=f"/telemetry/label/{label_id}"
    )


# --- Reads -------------------------------------------------------------------


def show_applied(node: BaseComponent, dut, op_param: str = "") -> Dict[str, Any]:
    """Parse ``nv show … --applied`` for ``node`` and always return a dict."""
    parsed = node.parse_show(op_param=op_param, dut_engine=dut, rev=ConfState.APPLIED)
    return parsed if isinstance(parsed, dict) else {}


# --- Writes ------------------------------------------------------------------


def create_resource(collection: BaseComponent, resource_id: str) -> None:
    """Create a keyed collection member (``destination <id>`` / ``stats-group <id>``)."""
    collection.set_resource(resource_id).verify_result()


def set_leaf(
    node: BaseComponent, dut, field: str, value: Any, *, apply: bool = False
) -> None:
    """Stage ``nv set … <field> <value>``, committing the candidate when ``apply``.

    ``value`` has to be a single CLI token: the NVUE wrapper interpolates it into
    the command unquoted, so whitespace is parsed as extra parameters.
    """
    if isinstance(value, str) and len(value.split()) != 1:
        raise ValueError(
            f"NVUE set value must be a single unquoted token, got {value!r} for "
            f"field {field!r} on {node.get_resource_path()}"
        )
    node.set(
        field, value, apply=apply, ask_for_confirmation=True, dut_engine=dut
    ).verify_result()


def unset_path(
    node: BaseComponent, dut, field: str = "", *, apply: bool = False
) -> None:
    """Stage ``nv unset … [<field>]``, committing the candidate when ``apply``."""
    node.unset(
        field, apply=apply, ask_for_confirmation=True, dut_engine=dut
    ).verify_result()


def enable_family_export(node: BaseComponent, dut, *, apply: bool = False) -> None:
    """Stage ``<family> export state enabled``."""
    set_leaf(node.export, dut, TelemetryConsts.STATE, _ENABLED, apply=apply)


def enable_family_phy_class(node: BaseComponent, dut, *, apply: bool = False) -> None:
    """Stage ``<family> class phy state enabled`` (interface / peer-port only)."""
    set_leaf(node.cls.phy, dut, TelemetryConsts.STATE, _ENABLED, apply=apply)


def stage_otlp_destination(
    system: System,
    dut,
    transport: str,
    dest_id: str,
    *,
    port: int,
    certificate: str,
    stats_group: str,
) -> TelemetryOtlpDestination:
    """Create an OTLP destination with its port, certificate and stats-group overrides.

    Stages every leaf in one candidate, commits once, and returns the destination node.
    """
    destinations = transport_node(system, transport).destination
    create_resource(destinations, dest_id)
    node = destination_node(system, transport, dest_id)
    set_leaf(node, dut, TelemetryConsts.PORT, port)
    set_leaf(node, dut, TelemetryConsts.CERTIFICATE, certificate)
    set_leaf(node, dut, TelemetryConsts.STATS_GROUP, stats_group, apply=True)
    return node


# --- Verification ------------------------------------------------------------


def verify_leaf(parsed: Dict[str, Any], field: str, expected: Any) -> None:
    """Verify ``parsed[field] == expected`` through the framework verifier."""
    ValidationTool.verify_field_value_in_output(parsed, field, expected).verify_result()


def verify_key_present(parsed: Dict[str, Any], key: str) -> None:
    """Verify ``key`` is present in a parsed ``nv show`` payload."""
    ValidationTool.verify_field_exist_in_json_output(parsed, [key]).verify_result()


def verify_key_absent(parsed: Dict[str, Any], key: str) -> None:
    """Verify ``key`` is no longer present in a parsed ``nv show`` payload."""
    ValidationTool.verify_field_exist_in_json_output(
        parsed, [key], should_be_found=False
    ).verify_result()


def verify_family_defaults(node: BaseComponent, dut, family: str) -> None:
    """Verify a metric family reads back its factory defaults.

    Asserts ``export state`` for every family, plus the family's ``sample-interval``
    at whichever node hosts it.
    """
    verify_leaf(
        show_applied(node.export, dut),
        TelemetryConsts.STATE,
        TelemetryConsts.Defaults.STATS_STATE,
    )
    if family == TelemetryConsts.PLATFORM_STATS:
        expected_interval = TelemetryConsts.Defaults.PLATFORM_EXPORT_SAMPLE_INTERVAL
    else:
        expected_interval = TelemetryConsts.Defaults.STATS_SAMPLE_INTERVAL
    verify_leaf(
        show_applied(sample_interval_node(family, node), dut),
        TelemetryConsts.SAMPLE_INTERVAL,
        expected_interval,
    )
