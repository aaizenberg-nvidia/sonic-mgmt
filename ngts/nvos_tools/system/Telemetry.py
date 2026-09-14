"""Infra for `nv … system telemetry …` commands.

Mirrors the composition pattern used by the rest of the codebase (e.g.
`ngts/nvos_tools/system/Ntp.py`, `ngts/nvos_tools/ib/InterfaceConfiguration/Interface.py`):
each subtree is a `BaseComponent` that owns its `path` fragment, and attaches
its children in `__init__`. Generic `show`/`set`/`unset` are inherited from
`BaseComponent`, so no per-command CLI wiring is needed.

The "stats" subtrees (`interface-stats`, `peer-port-stats`,
`ib-router-stats`, `platform-stats`, `software-stats`) live BOTH at the
telemetry root and inside every `stats-group <id>`. The same classes are
instantiated under both parents.
"""
import logging

import allure

from ngts.nvos_constants.constants_nvos import ApiType, TelemetryConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit


# =========================================================================
# Generic collection helper
# =========================================================================

class _CollectionBase(BaseComponent):
    """Generic parent for `/<collection>` nodes whose children are keyed by id.

    Used for `export/otlp/<protocol>/destination`, `label`, and `stats-group`.
    Follows the same pattern as `NtpBaseResources` in
    `ngts/nvos_tools/system/Ntp.py`.
    """

    def __init__(self, parent_obj=None, path=''):
        BaseComponent.__init__(self, parent=parent_obj, path=path)
        self.resources_dict = {}

    def _create_resource(self, resource_id):
        """Override in subclasses to return the per-id child instance to cache."""
        return BaseComponent(self, path='/' + resource_id)

    def set_resource(self, resource_id, expected_str='', apply=False, ask_for_confirmation=False):
        with allure.step("Set {} with id : {}".format(self._resource_path, resource_id)):
            logging.info("Set %s with id : %s", self._resource_path, resource_id)
            # NVUE expects an empty string suffix; OpenAPI expects an empty object body.
            resource_value = {} if TestToolkit.tested_api == ApiType.OPENAPI else ""
            result_obj = self.set(op_param_name=resource_id, op_param_value=resource_value,
                                  expected_str=expected_str, apply=apply,
                                  ask_for_confirmation=ask_for_confirmation)
            child = self._create_resource(resource_id)
            self.resources_dict[resource_id] = child
            return result_obj

    def unset_resource(self, resource_id, apply=False, ask_for_confirmation=False):
        if resource_id not in self.resources_dict:
            raise ValueError(f"Resource '{resource_id}' not found in {self._resource_path}")
        result_obj = self.resources_dict[resource_id].unset(
            apply=apply, ask_for_confirmation=ask_for_confirmation)
        self.resources_dict.pop(resource_id)
        return result_obj


# =========================================================================
# Stats subtrees: interface-stats, peer-port-stats, ib-router-stats
# (platform-stats is standalone — see below)
# =========================================================================

class _StatsExportState(BaseComponent):
    """`<stats>/export` parent. Settable: `state`."""

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/export')


class _StatsClassPhy(BaseComponent):
    """`<stats>/class` parent containing `phy` (settable `state`)."""

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/class')
        self.phy = BaseComponent(self, path='/phy')


class _StatsBase(BaseComponent):
    """Base for interface-stats / peer-port-stats / ib-router-stats.

    Provides `<root>/export/state` and a settable `sample-interval` (1-86400)
    at the root. Subclasses set the `path` fragment.
    """

    def __init__(self, parent_obj, path):
        BaseComponent.__init__(self, parent=parent_obj, path=path)
        self.export = _StatsExportState(self)


class _StatsWithPhyClass(_StatsBase):
    """Adds `class/phy/state` to `_StatsBase`. Used by interface/peer-port stats."""

    def __init__(self, parent_obj, path):
        _StatsBase.__init__(self, parent_obj=parent_obj, path=path)
        self.cls = _StatsClassPhy(self)


class InterfaceStats(_StatsWithPhyClass):
    """`telemetry/interface-stats` subtree (also reused under `stats-group/<id>`)."""

    def __init__(self, parent_obj):
        _StatsWithPhyClass.__init__(self, parent_obj=parent_obj, path='/interface-stats')


class PeerPortStats(_StatsWithPhyClass):
    """`telemetry/peer-port-stats` subtree (also reused under `stats-group/<id>`)."""

    def __init__(self, parent_obj):
        _StatsWithPhyClass.__init__(self, parent_obj=parent_obj, path='/peer-port-stats')


class IbRouterStats(_StatsBase):
    """`telemetry/ib-router-stats` subtree (also reused under `stats-group/<id>`)."""

    def __init__(self, parent_obj):
        _StatsBase.__init__(self, parent_obj=parent_obj, path='/ib-router-stats')


# =========================================================================
# platform-stats (standalone — `sample-interval` lives on `export` and on
# each `class.<cat>`, range 60-86400; no top-level `sample-interval`).
# =========================================================================

class _PlatformStatsExport(BaseComponent):
    """`platform-stats/export` — settable `state` and `sample-interval` (60-86400)."""

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/export')


class _PlatformStatsClassCategory(BaseComponent):
    """A single `platform-stats/class/<cat>` node.

    Settable: `state`, `sample-interval` (60-86400).
    """

    def __init__(self, parent_obj, category):
        BaseComponent.__init__(self, parent=parent_obj, path='/' + category)
        self.category = category


class _PlatformStatsClass(BaseComponent):
    """`platform-stats/class` parent with one child per known platform class.

    Children are stored in `self.categories`, keyed by class name (e.g. 'cpu').
    """

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/class')
        self.categories = {
            cat: _PlatformStatsClassCategory(self, cat)
            for cat in TelemetryConsts.PLATFORM_CLASSES
        }


class PlatformStats(BaseComponent):
    """`telemetry/platform-stats` subtree (also reused under `stats-group/<id>`).

    Note: unlike the other stats subtrees, `platform-stats` has no top-level
    `sample-interval` setting — it lives under `export` and under each
    `class.<cat>` child.
    """

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/platform-stats')
        self.export = _PlatformStatsExport(self)
        self.cls = _PlatformStatsClass(self)


# =========================================================================
# software-stats / systemd (export + unit-profile/<id>/unit/<id>)
# =========================================================================

class _SoftwareStatsSystemdExport(BaseComponent):
    """`software-stats/systemd/export` — settable leaf: `state` (enabled|disabled)."""

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/export')


class SoftwareStatsUnitProfileUnits(_CollectionBase):
    """`software-stats/systemd/unit-profile/<id>/unit` collection.

    Each `<unit-id>` is a keyed member set with an empty body (see `_CollectionBase`).
    """

    def __init__(self, parent_obj):
        _CollectionBase.__init__(self, parent_obj=parent_obj, path='/unit')


class SoftwareStatsUnitProfile(BaseComponent):
    """A single `software-stats/systemd/unit-profile/<id>` instance. Child: `unit`."""

    def __init__(self, parent_obj, profile_id):
        BaseComponent.__init__(self, parent=parent_obj, path='/' + profile_id)
        self.profile_id = profile_id
        self.unit = SoftwareStatsUnitProfileUnits(self)


class SoftwareStatsUnitProfiles(_CollectionBase):
    """`software-stats/systemd/unit-profile` collection of `SoftwareStatsUnitProfile`."""

    def __init__(self, parent_obj):
        _CollectionBase.__init__(self, parent_obj=parent_obj, path='/unit-profile')

    def _create_resource(self, resource_id):
        return SoftwareStatsUnitProfile(self, resource_id)


class _SoftwareStatsSystemd(BaseComponent):
    """`software-stats/systemd`.

    Settable leaves: `active-profile`, `process-level` (enabled|disabled),
    `sample-interval`. Children: `export`, `unit-profile`.
    """

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/systemd')
        self.export = _SoftwareStatsSystemdExport(self)
        self.unit_profile = SoftwareStatsUnitProfiles(self)


class SoftwareStats(BaseComponent):
    """`telemetry/software-stats` subtree (also reused under `stats-group/<id>`).

    Child: `systemd`.
    """

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/software-stats')
        self.systemd = _SoftwareStatsSystemd(self)

# =========================================================================
# export / otlp / grpc|http / destination
# =========================================================================


class TelemetryOtlpDestination(BaseComponent):
    """Single `<protocol>/destination/<id>` instance.

    Settable leaves: `stats-group`, `certificate`, `client-certificate`, `port`.
    """

    def __init__(self, parent_obj, destination_id):
        BaseComponent.__init__(self, parent=parent_obj, path='/' + destination_id)
        self.destination_id = destination_id


class TelemetryOtlpDestinations(_CollectionBase):
    """`<protocol>/destination` collection of `TelemetryOtlpDestination` entries."""

    def __init__(self, parent_obj):
        _CollectionBase.__init__(self, parent_obj=parent_obj, path='/destination')

    def _create_resource(self, resource_id):
        return TelemetryOtlpDestination(self, resource_id)


class TelemetryOtlpGrpc(BaseComponent):
    """`telemetry/export/otlp/grpc`.

    Settable leaves: `insecure`, `certificate`, `client-certificate`, `port`.
    Child: `destination` collection.
    """

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/grpc')
        self.destination = TelemetryOtlpDestinations(self)


class TelemetryOtlpHttp(BaseComponent):
    """`telemetry/export/otlp/http`.

    Settable leaves: `insecure`, `certificate`, `client-certificate`, `port`,
    `encoding` (proto|json). Child: `destination` collection.
    """

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/http')
        self.destination = TelemetryOtlpDestinations(self)


class TelemetryOtlp(BaseComponent):
    """`telemetry/export/otlp`.

    Settable leaf: `state` (enabled|disabled). Children: `grpc`, `http`.
    """

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/otlp')
        self.grpc = TelemetryOtlpGrpc(self)
        self.http = TelemetryOtlpHttp(self)


class TelemetryExport(BaseComponent):
    """`telemetry/export`.

    Settable leaf: `vrf`. Child: `otlp`.
    """

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/export')
        self.otlp = TelemetryOtlp(self)


# =========================================================================
# logs / export  (syslog-over-OTEL)
# =========================================================================

class TelemetryLogsExport(BaseComponent):
    """`telemetry/logs/export`.

    Settable leaf: `state` (enabled|disabled) — the syslog-over-OTEL master knob
    (`nv set system telemetry logs export state …`). Forwarding is only active
    once OTLP export with a destination is also configured.
    """

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/export')


class TelemetryLogs(BaseComponent):
    """`telemetry/logs`. Child: `export`."""

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/logs')
        self.export = TelemetryLogsExport(self)


# =========================================================================
# label / <label-id>
# =========================================================================

class TelemetryLabel(BaseComponent):
    """A single `telemetry/label/<id>` instance. Settable leaf: `description`."""

    def __init__(self, parent_obj, label_id):
        BaseComponent.__init__(self, parent=parent_obj, path='/' + label_id)
        self.label_id = label_id


class TelemetryLabels(_CollectionBase):
    """`telemetry/label` collection of `TelemetryLabel` entries."""

    def __init__(self, parent_obj):
        _CollectionBase.__init__(self, parent_obj=parent_obj, path='/label')

    def _create_resource(self, resource_id):
        return TelemetryLabel(self, resource_id)


# =========================================================================
# stats-group / <id> — hosts the same stats subtree shape as telemetry root
# =========================================================================

class TelemetryStatsGroup(BaseComponent):
    """A single `telemetry/stats-group/<id>` instance.

    Hosts the same `interface-stats`, `peer-port-stats`, `ib-router-stats`
    and `platform-stats` subtree shape as the top-level telemetry node.
    """

    def __init__(self, parent_obj, group_id):
        BaseComponent.__init__(self, parent=parent_obj, path='/' + group_id)
        self.group_id = group_id
        self.interface_stats = InterfaceStats(self)
        self.peer_port_stats = PeerPortStats(self)
        self.ib_router_stats = IbRouterStats(self)
        self.platform_stats = PlatformStats(self)
        self.software_stats = SoftwareStats(self)


class TelemetryStatsGroups(_CollectionBase):
    """`telemetry/stats-group` collection of `TelemetryStatsGroup` entries."""

    def __init__(self, parent_obj):
        _CollectionBase.__init__(self, parent_obj=parent_obj, path='/stats-group')

    def _create_resource(self, resource_id):
        return TelemetryStatsGroup(self, resource_id)


# =========================================================================
# Root: telemetry
# =========================================================================

class Telemetry(BaseComponent):
    """Root telemetry subtree: `nv … system telemetry …`.

    Attached to `System` as `self.telemetry`. Generic `show/set/unset`
    inherited from `BaseComponent` cover the entire surface.
    """

    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/telemetry')
        self.export = TelemetryExport(self)
        self.logs = TelemetryLogs(self)
        self.label = TelemetryLabels(self)
        self.interface_stats = InterfaceStats(self)
        self.peer_port_stats = PeerPortStats(self)
        self.ib_router_stats = IbRouterStats(self)
        self.platform_stats = PlatformStats(self)
        self.software_stats = SoftwareStats(self)
        self.stats_group = TelemetryStatsGroups(self)
