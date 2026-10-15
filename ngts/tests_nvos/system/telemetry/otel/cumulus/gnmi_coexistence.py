"""gNMI dial-out coexistence helpers (SSIM ``test_telemetry_coexistence.py`` parity on mlx lab)."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.EngineAdapterTool import EngineAdapterTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_constants.constants_nvos import ImageConsts, PlatformConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.system.Telemetry import SoftwareStatsUnitProfile
from ngts.nvos_tools.vrf.vrf import Vrf
from ngts.tests_nvos.system.grpc_tunnel.grpcTunnelServer import GrpcTunnelServer
from ngts.tests_nvos.system.grpc_tunnel.helpers import GrpcTunnelServerSetup
from ngts.tests_nvos.system.telemetry.otel.cumulus import cache as telemetryCache
from ngts.tests_nvos.system.telemetry.otel.cumulus.catalog import detect_nvue_python_pkg_version
from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import dut_root_on_nvme_storage
from ngts.tests_nvos.system.telemetry.otel.cumulus.interface_xpaths import (
    build_interface_top_level_xpaths,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.system_xpaths import (
    build_system_top_level_xpaths,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.components_xpaths import (
    BASIC_LEAVES,
    COMPONENT_TYPES,
    ComponentType,
    PSU_BAD_ABSENT_LEAVES,
    build_components_top_level_xpaths,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.lab_topology import (
    discover_swp_interfaces_on_dut,
    resolve_cumulus_lab_interfaces_on_dut,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.qos_xpaths import (
    filter_unsupported_qos_xpaths,
    generate_qos_container_xpaths,
)

logger = logging.getLogger(__name__)

# SSIM smoke dial-out ports (``Test_Telemetry_Coexistence_Smoke``).
SMOKE_GNMI_PORT_LLDP = 57358
SMOKE_GNMI_PORT_BGP = 57713
# Smoke test 07/08 top-level subscription dial-out ports. These MUST stay distinct
# from the Interface quality-of-service (57421) and Component/System (57431/57432) suite ports
# and use dedicated tunnel names: the smoke fixture keeps its gNMI session alive for the whole
# module, so a shared tunnel name and listen port would collide with those later collections
# (collector bind conflict, DUT gRPC tunnel reconfiguration clash, running cleanup twice,
# and a shared sample log path).
SMOKE_GNMI_PORT_INTERFACE_TOP = 57441
SMOKE_GNMI_PORT_SYSTEM_TOP = 57451
# Smoke test 09 /components top-level subscription dial-out port (dedicated tunnel; must
# stay distinct from the other smoke/suite ports for the same deferred-cleanup reasons as above).
SMOKE_GNMI_PORT_COMPONENT_TOP = 57461

SMOKE_BGP_ATTRS: Tuple[str, ...] = (
    'established_transitions', 'input_queue', 'messages_received', 'messages_sent', 'output_queue',
    'prefixes_installed', 'prefixes_received', 'prefixes_sent', 'session_state', 'local_as', 'last_established',
    'last_notification_error_code_sent', 'last_notification_error_code_received', 'peer_as', 'peer_type',
    'neighbor_add', 'description', 'peer_group',
)

SMOKE_LLDP_ATTRS: Tuple[str, ...] = (
    'lldp_state_enabled', 'interface_lldp_state_enabled', 'neighbor_ttl', 'neighbor_port_id_type',
    'chassis_id', 'chassis_id_type', 'system_name', 'system_description', 'neighbor_age', 'neighbor_port_id',
    'neighbor_chassis_id', 'neighbor_chassis_id_type', 'neighbor_system_name', 'neighbor_system_description',
    'neighbor_management_address_type', 'neighbor_port_description', 'neighbor_router', 'neighbor_telephone',
    'neighbor_other', 'neighbor_repeater', 'neighbor_mac_bridge', 'neighbor_wlan_access_point',
    'neighbor_docsis_cable_device', 'neighbor_station_only', 'neighbor_c_vlan', 'neighbor_s_vlan',
    'neighbor_two_port_mac_relay',
)

INTERFACE_QOS_GNMI_ATTRS: Tuple[str, ...] = (
    'in_broadcast_pkts', 'in_multicast_pkts', 'in_octets', 'in_errors',
)

SYSTEM_GNMI_ATTRS: Tuple[str, ...] = ('cp_out_ipv4_pkts', 'cp_out_ipv6_pkts')

COMPONENT_GNMI_ATTRS: Tuple[str, ...] = ('storage_read_bytes', 'storage_write_seconds')

# SSIM Component/System ``test_09`` — OpenConfig CPU component state leaves
# (``/components/component[name=CPU]/state/...``), collected by a dedicated
# ``json_ietf`` component subscription like the SSIM CPU gNMI job.
CPU_COMPONENT_NAME = 'CPU'
CPU_COMPONENT_GNMI_MODEL = 'component-cpu'
CPU_COMPONENT_GNMI_SERIES_KEY = 'gnmi-series-%s' % (CPU_COMPONENT_GNMI_MODEL,)
CPU_COMPONENT_GNMI_ENCODING = 'json_ietf'
CPU_COMPONENT_GNMI_LISTEN_PORT = 57433
CPU_PLATFORM_HW_GNMI_ATTRS: Tuple[str, ...] = (
    'cpu_serial_no',
    'cpu_part_no',
    'cpu_model_name',
    'cpu_hardware_version',
    'cpu_type',
)

# SSIM Component/System ``test_10`` — OpenConfig PSU component state leaves for one
# randomly chosen present PSU (``/components/component[name=<psu>]/state/...``).
PSU_COMPONENT_GNMI_MODEL = 'component-psu'
PSU_COMPONENT_GNMI_SERIES_KEY = 'gnmi-series-%s' % (PSU_COMPONENT_GNMI_MODEL,)
PSU_COMPONENT_GNMI_ENCODING = 'json_ietf'
PSU_COMPONENT_GNMI_LISTEN_PORT = 57434
PSU_PLATFORM_HW_GNMI_ATTRS: Tuple[str, ...] = (
    'psu_serial_no',
    'psu_part_no',
    'psu_model_name',
    'psu_hardware_version',
    'psu_type',
)
# ``component[name=…]`` state leaf per PSU attr. The subscription omits the list key because the
# DUT spells PSU components differently than NVUE inventory (``PSU 1`` vs ``PSU1``); the sample is
# resolved back to the chosen PSU by :func:`latest_gnmi_component_state_leaf`.
PSU_STATE_LEAF_BY_ATTR: Dict[str, str] = {
    'psu_serial_no': 'serial-no',
    'psu_part_no': 'part-no',
    'psu_model_name': 'model-name',
    'psu_hardware_version': 'hardware-version',
    'psu_type': 'type',
}
# Chosen during ``build_component_system_gnmi_jobs`` so the CLI leg uses the same name.
_SELECTED_PSU_COMPONENT_NAME: Optional[str] = None

_GNMI_JSON_DECODER = json.JSONDecoder()
_GNMIC_INSTALL_VERSION = '0.38.2'

# NVOS starts the dial-out client as a docker container from ``nv config apply``, so the
# client is not up when the apply returns.
_GRPC_TUNNEL_CONTAINER_WAIT_SEC = 30
_GRPC_TUNNEL_CONTAINER_POLL_SEC = 2
# Characters of DUT command output kept in a failure message.
_DUT_OUTPUT_EXCERPT_CHARS = 800


@dataclass
class GnmiCollectionJob:
    """One gRPC tunnel + gnmic sample subscription."""

    model: str
    tunnel_name: str
    listen_port: int
    paths: List[str]
    sample_interval: str
    duration_sec: int
    attrs: Tuple[str, ...] = ()
    populate_kwargs: Dict[str, Any] = field(default_factory=dict)
    group_size: int = 1
    encoding: str = 'json'
    # Best-effort job: an empty sample log is logged and skipped instead of failing collection,
    # so one unsupported component subtree cannot break the whole suite cache.
    optional: bool = False


@dataclass
class GnmiCoexistenceSession:
    setups: List[GrpcTunnelServerSetup] = field(default_factory=list)
    gnmic_bin: str = 'gnmic'
    mtls_configured: bool = False
    is_nvos: bool = False


def _address_family_key(afi_safi_name: str) -> str:
    if afi_safi_name == 'L2VPN_EVPN':
        return 'l2vpn-evpn'
    if afi_safi_name == 'IPV6_UNICAST':
        return 'ipv6-unicast'
    return 'ipv4-unicast'


def _bgp_xpath(attr: str, *, vrf: str, neighbor_address: str, afi_safi_name: str) -> Optional[str]:
    base = (
        f'/network-instances/network-instance[name={vrf}]/protocols/protocol[identifier=BGP][name=BGP]'
        f'/bgp/neighbors/neighbor[neighbor-address={neighbor_address}]'
    )
    af_key = _address_family_key(afi_safi_name)
    mapping = {
        'prefixes_received': f'{base}/afi-safis/afi-safi[afi-safi-name={afi_safi_name}]/state/prefixes/received',
        'prefixes_installed': f'{base}/afi-safis/afi-safi[afi-safi-name={afi_safi_name}]/state/prefixes/installed',
        'prefixes_sent': f'{base}/afi-safis/afi-safi[afi-safi-name={afi_safi_name}]/state/prefixes/sent',
        'established_transitions': f'{base}/state/established-transitions',
        'messages_sent': f'{base}/state/messages/sent/UPDATE',
        'messages_received': f'{base}/state/messages/received/UPDATE',
        'session_state': f'{base}/state/session-state',
        'input_queue': f'{base}/state/queues/input',
        'output_queue': f'{base}/state/queues/output',
        'last_established': f'{base}/state/last-established',
        'local_as': f'{base}/state/local-as',
        'peer_as': f'{base}/state/peer-as',
        'description': f'{base}/state/description',
        'peer_group': f'{base}/state/peer-group',
        'peer_type': f'{base}/state/peer-type',
        'last_notification_error_code_sent': f'{base}/state/messages/sent/last-notification-error-code',
        'last_notification_error_code_received': f'{base}/state/messages/received/last-notification-error-code',
        'neighbor_add': f'{base}/state/neighbor-address',
    }
    return mapping.get(attr)


def _lldp_xpath(attr: str, *, interface: str, neighbor_id: str, port: str, mgmt_address: Optional[str]) -> Optional[str]:
    nb = f'/lldp/interfaces/interface[name={interface}]/neighbors/neighbor[id={neighbor_id}:{port}]'
    mgmt = mgmt_address or '0.0.0.0'
    mapping = {
        'lldp_state_enabled': '/lldp/state/enabled',
        'interface_lldp_state_enabled': f'/lldp/interfaces/interface[name={interface}]/state/enabled',
        'chassis_id': '/lldp/state/chassis-id',
        'chassis_id_type': '/lldp/state/chassis-id-type',
        'system_description': '/lldp/state/system-description',
        'system_name': '/lldp/state/system-name',
        'neighbor_age': f'{nb}/state/age',
        'neighbor_port_id': f'{nb}/state/port-id',
        'neighbor_chassis_id': f'{nb}/state/chassis-id',
        'neighbor_chassis_id_type': f'{nb}/state/chassis-id-type',
        'neighbor_system_name': f'{nb}/state/system-name',
        'neighbor_system_description': f'{nb}/state/system-description',
        'neighbor_management_address_type': f'{nb}/state/management-addresses[address={mgmt}]/type',
        'neighbor_port_description': f'{nb}/state/port-description',
        'neighbor_port_id_type': f'{nb}/state/port-id-type',
        'neighbor_ttl': f'{nb}/state/ttl',
        'neighbor_router': f'{nb}/capabilities/capability[name=ROUTER]/state/enabled',
        'neighbor_telephone': f'{nb}/capabilities/capability[name=TELEPHONE]/state/enabled',
        'neighbor_other': f'{nb}/capabilities/capability[name=OTHER]/state/enabled',
        'neighbor_repeater': f'{nb}/capabilities/capability[name=REPEATER]/state/enabled',
        'neighbor_mac_bridge': f'{nb}/capabilities/capability[name=MAC_BRIDGE]/state/enabled',
        'neighbor_wlan_access_point': f'{nb}/capabilities/capability[name=WLAN_ACCESS_POINT]/state/enabled',
        'neighbor_docsis_cable_device': f'{nb}/capabilities/capability[name=DOCSIS_CABLE_DEVICE]/state/enabled',
        'neighbor_station_only': f'{nb}/capabilities/capability[name=STATION_ONLY]/state/enabled',
        'neighbor_c_vlan': f'{nb}/capabilities/capability[name=C_VLAN]/state/enabled',
        'neighbor_s_vlan': f'{nb}/capabilities/capability[name=S_VLAN]/state/enabled',
        'neighbor_two_port_mac_relay': f'{nb}/capabilities/capability[name=TWO_PORT_MAC_RELAY]/state/enabled',
    }
    return mapping.get(attr)


def _interface_xpath(attr: str, *, interface: str) -> Optional[str]:
    mapping = {
        'in_broadcast_pkts': f'/interfaces/interface[name={interface}]/state/counters/in-broadcast-pkts',
        'in_multicast_pkts': f'/interfaces/interface[name={interface}]/state/counters/in-multicast-pkts',
        'in_octets': f'/interfaces/interface[name={interface}]/state/counters/in-octets',
        'in_errors': f'/interfaces/interface[name={interface}]/state/counters/in-errors',
        'description': f'/interfaces/interface[name={interface}]/state/description',
    }
    return mapping.get(attr)


def interface_top_level_xpath(interface: str) -> str:
    """SSIM3 interface top-level path — the whole interface tree.

    ``test_07_interface_top_level_subscription`` subscribes to this single path and
    expects every modeled interface leaf (see
    ``interface_xpaths.build_interface_top_level_xpaths``) to be streamed.
    """
    return f'/interfaces/interface[name={interface}]'


def system_top_level_xpath() -> str:
    """SSIM3 system top-level path — the whole ``/system`` tree.

    ``test_08_system_top_level_subscription`` subscribes to this single path and expects
    every modeled system leaf (see
    ``system_xpaths.build_system_top_level_xpaths``) to be streamed.
    """
    return '/system'


def components_top_level_xpath() -> str:
    """SSIM3 components top-level path — the whole ``/components`` tree.

    ``test_09_components_top_level_subscription`` subscribes to this single path and expects
    every modeled component leaf (see
    ``components_xpaths.build_components_top_level_xpaths``) to be streamed.
    """
    return '/components'


def _system_xpath(attr: str) -> Optional[str]:
    mapping = {
        'cp_out_ipv4_pkts': '/system/control-plane-traffic/egress/ipv4/counters/out-ipv4-pkts',
        'cp_out_ipv6_pkts': '/system/control-plane-traffic/egress/ipv6/counters/out-ipv6-pkts',
    }
    return mapping.get(attr)


def _component_xpath(attr: str, *, component_name: str) -> Optional[str]:
    mapping = {
        'storage_read_bytes': f'/components/component[name={component_name}]/storage/state/counters/read-bytes',
        'storage_write_seconds': f'/components/component[name={component_name}]/storage/state/counters/write-seconds',
        # SSIM test_09 CPU platform hw info (relative to component state).
        'cpu_serial_no': f'/components/component[name={component_name}]/state/serial-no',
        'cpu_part_no': f'/components/component[name={component_name}]/state/part-no',
        'cpu_model_name': f'/components/component[name={component_name}]/state/model-name',
        'cpu_hardware_version': f'/components/component[name={component_name}]/state/hardware-version',
        'cpu_type': f'/components/component[name={component_name}]/state/type',
        # SSIM test_10 PSU platform hw info (relative to component state).
        'psu_serial_no': f'/components/component[name={component_name}]/state/serial-no',
        'psu_part_no': f'/components/component[name={component_name}]/state/part-no',
        'psu_model_name': f'/components/component[name={component_name}]/state/model-name',
        'psu_hardware_version': f'/components/component[name={component_name}]/state/hardware-version',
        'psu_type': f'/components/component[name={component_name}]/state/type',
    }
    return mapping.get(attr)


def resolve_model_xpaths(model: str, attrs: Sequence[str], populate_kwargs: Dict[str, Any]) -> List[str]:
    """Map SSIM attr names to OpenConfig/Cumulus subscribe paths."""
    paths: List[str] = []
    kwargs = populate_kwargs or {}
    for attr in attrs or []:
        xp = None
        if model == 'bgp':
            xp = _bgp_xpath(
                attr,
                vrf=kwargs.get('vrf', 'default'),
                neighbor_address=kwargs['neighbor_address'],
                afi_safi_name=kwargs.get('afi_safi_name', 'IPV4_UNICAST'),
            )
        elif model == 'lldp':
            xp = _lldp_xpath(
                attr,
                interface=kwargs['interface'],
                neighbor_id=kwargs['neighbor_id'],
                port=kwargs.get('port', kwargs['interface']),
                mgmt_address=kwargs.get('mgmt_address'),
            )
        elif model == 'interface':
            xp = _interface_xpath(attr, interface=kwargs['interface'])
        elif model == 'system':
            xp = _system_xpath(attr)
        elif model == 'component':
            xp = _component_xpath(attr, component_name=kwargs['component_name'])
        if xp and xp not in paths:
            paths.append(xp)
    return paths


def _normalize_xpath(xpath: Optional[str]) -> Optional[str]:
    if not isinstance(xpath, str):
        return xpath
    return xpath.lstrip('/')


def _entry_numeric_value(entry: Any) -> Optional[float]:
    """First numeric leaf value from a ``gnmi-series`` entry (dict/scalar/str)."""
    if isinstance(entry, bool):
        return None
    if isinstance(entry, (int, float)):
        return float(entry)
    if isinstance(entry, str):
        try:
            return float(entry)
        except ValueError:
            return None
    if isinstance(entry, dict):
        for key, value in entry.items():
            if key == 'xpath' or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    continue
    return None


def gnmi_series_numeric_samples(
    series: Optional[Dict[str, Dict[str, Any]]], xpath: Optional[str]
) -> List[Tuple[int, float]]:
    """Return ``[(timestamp, value), ...]`` for ``xpath`` across a ``gnmi-series-*`` cache."""
    target = _normalize_xpath(xpath)
    samples: List[Tuple[int, float]] = []
    if not isinstance(series, dict) or not target:
        return samples
    for ts_key, bucket in series.items():
        try:
            ts = int(ts_key)
        except (TypeError, ValueError):
            continue
        if not isinstance(bucket, dict):
            continue
        for xp, entry in bucket.items():
            candidate = xp
            if isinstance(entry, dict) and isinstance(entry.get('xpath'), str):
                candidate = entry['xpath']
            if _normalize_xpath(xp) == target or _normalize_xpath(candidate) == target:
                value = _entry_numeric_value(entry)
                if value is not None:
                    samples.append((ts, value))
                break
    samples.sort(key=lambda item: item[0])
    return samples


def latest_gnmi_numeric_for_xpath(
    series: Optional[Dict[str, Dict[str, Any]]], xpath: Optional[str]
) -> Optional[float]:
    """Latest numeric value for ``xpath`` from a ``gnmi-series-*`` cache, or ``None``."""
    samples = gnmi_series_numeric_samples(series, xpath)
    return samples[-1][1] if samples else None


def latest_gnmi_leaf_for_xpath(
    series: Optional[Dict[str, Dict[str, Any]]], xpath: Optional[str]
) -> Optional[Any]:
    """Latest leaf value (string or number) for ``xpath`` from a ``gnmi-series-*`` cache."""
    target = _normalize_xpath(xpath)
    if not isinstance(series, dict) or not target:
        return None
    latest_ts = None
    latest_value: Optional[Any] = None
    for ts_key, bucket in series.items():
        try:
            ts = int(ts_key)
        except (TypeError, ValueError):
            continue
        if not isinstance(bucket, dict):
            continue
        for xp, entry in bucket.items():
            candidate = xp
            if isinstance(entry, dict) and isinstance(entry.get('xpath'), str):
                candidate = entry['xpath']
            if _normalize_xpath(xp) != target and _normalize_xpath(candidate) != target:
                continue
            leaves = _entry_leaf_values(entry)
            if not leaves:
                continue
            if latest_ts is None or ts >= latest_ts:
                latest_ts = ts
                latest_value = leaves[0]
    return latest_value


def normalize_component_name(name: Optional[str]) -> str:
    """``PSU 1`` / ``psu1`` / ``PSU-1`` → ``PSU1`` for cross-source component name matching."""
    return re.sub(r'[^A-Z0-9]', '', str(name or '').upper())


def _xpath_component_name(xpath: Optional[str]) -> Optional[str]:
    match = re.search(r'component\[name=([^\]]+)\]', str(xpath or ''))
    return match.group(1) if match else None


def gnmi_component_names_in_series(series: Optional[Dict[str, Dict[str, Any]]]) -> List[str]:
    """Component names present in a ``gnmi-series-*`` cache (diagnostics)."""
    names: List[str] = []
    if not isinstance(series, dict):
        return names
    for bucket in series.values():
        if not isinstance(bucket, dict):
            continue
        for xp, entry in bucket.items():
            candidate = xp
            if isinstance(entry, dict) and isinstance(entry.get('xpath'), str):
                candidate = entry['xpath']
            name = _xpath_component_name(candidate) or _xpath_component_name(xp)
            if name and name not in names:
                names.append(name)
    return sorted(names)


def latest_gnmi_component_state_leaf(
    series: Optional[Dict[str, Dict[str, Any]]],
    *,
    component_name: str,
    leaf: str,
) -> Tuple[Optional[Any], Optional[str]]:
    """Latest ``component[name~=component_name]/state/<leaf>`` value and the xpath it came from.

    The component name is matched after normalization so a DUT spelling such as ``PSU 1`` matches
    the NVUE inventory key ``PSU1``.
    """
    target_name = normalize_component_name(component_name)
    suffix = '/state/%s' % (leaf,)
    if not isinstance(series, dict) or not target_name or not leaf:
        return None, None
    latest_ts = None
    latest_value: Optional[Any] = None
    latest_xpath: Optional[str] = None
    for ts_key, bucket in series.items():
        try:
            ts = int(ts_key)
        except (TypeError, ValueError):
            continue
        if not isinstance(bucket, dict):
            continue
        for xp, entry in bucket.items():
            candidate = xp
            if isinstance(entry, dict) and isinstance(entry.get('xpath'), str):
                candidate = entry['xpath']
            if not str(candidate).endswith(suffix):
                continue
            name = _xpath_component_name(candidate)
            if normalize_component_name(name) != target_name:
                continue
            leaf_value = _scalar_for_leaf(entry, leaf)
            if leaf_value is None:
                continue
            if latest_ts is None or ts >= latest_ts:
                latest_ts = ts
                latest_value = leaf_value
                latest_xpath = candidate
    return latest_value, latest_xpath


def _parse_json_objects_from_gnmic_log(content: str) -> List[Dict[str, Any]]:
    """Extract top-level JSON objects from gnmic ``-d`` output (SSIM ``multilinedict_to_listofdict``)."""
    objects: List[Dict[str, Any]] = []
    idx = 0
    length = len(content)
    while idx < length:
        pos = content.find('{', idx)
        if pos == -1:
            break
        if pos != 0 and content[pos - 1] != '\n':
            idx = pos + 1
            continue
        try:
            obj, end_idx = _GNMI_JSON_DECODER.raw_decode(content, pos)
            if isinstance(obj, dict):
                objects.append(obj)
            idx = end_idx
        except json.JSONDecodeError:
            next_nl = content.find('\n', pos)
            if next_nl == -1:
                break
            idx = next_nl + 1
    return objects


def _is_sync_response(obj: Dict[str, Any]) -> bool:
    return bool(obj.get('sync_response'))


def _notification_timestamp(obj: Dict[str, Any]) -> Optional[int]:
    for key in ('timestamp', 'time'):
        if key in obj:
            try:
                raw = int(obj[key])
            except (TypeError, ValueError):
                continue
            if raw > 1_000_000_000_000:
                return raw // 1_000_000_000
            return raw
    return None


def _combine_prefix_path(prefix: str, path: str) -> str:
    pfx = (prefix or '').strip('/')
    pth = (path or '').strip('/')
    if pfx and pth:
        return f'/{pfx}/{pth}'
    if pfx:
        return f'/{pfx}'
    if pth:
        return f'/{pth}' if pth.startswith('/') else f'/{pth}'
    return ''


def _extract_updates(obj: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    prefix = obj.get('prefix', '')
    updates = obj.get('updates') or []
    if not isinstance(updates, list):
        return out
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        path = upd.get('Path') or upd.get('path') or ''
        if prefix:
            xpath = _combine_prefix_path(str(prefix), str(path))
        else:
            xpath = str(path) if str(path).startswith('/') else f'/{path}'
        if not xpath:
            continue
        values = upd.get('values') or upd.get('val') or {}
        entry: Dict[str, Any] = {'xpath': xpath}
        if isinstance(values, dict):
            entry.update(values)
        out[xpath] = entry
    return out


def parse_gnmic_log_to_series(log_content: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Build ``gnmi-series-{model}`` shape: ``{timestamp: {xpath: entry}}``."""
    series: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for obj in _parse_json_objects_from_gnmic_log(log_content or ''):
        if _is_sync_response(obj):
            continue
        ts = _notification_timestamp(obj)
        if ts is None:
            continue
        ts_key = str(ts)
        entries = _extract_updates(obj)
        if not entries:
            continue
        bucket = series.setdefault(ts_key, {})
        for xpath, entry in entries.items():
            bucket[xpath] = entry
    return series


def _parse_interval_to_seconds(sample_interval: Any) -> Optional[float]:
    if sample_interval is None:
        return None
    if isinstance(sample_interval, (int, float)):
        return float(sample_interval)
    text = str(sample_interval).strip().lower()
    if text.endswith('s'):
        try:
            return float(text[:-1])
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def validate_gnmi_sample_interval_per_timestamp(
    *,
    models: Sequence[str],
    sample_interval: Any,
    tolerance: float = 0.25,
    series_key_prefix: str = 'gnmi-series-',
    cache_key: str = 'gnmi_timing',
) -> None:
    """SSIM ``validate_gnmi_sample_interval_per_timestamp`` (stores summary in cache).

    SSIM fails only when the *median* consecutive gap is outside ``[min_gap, max_gap]``.
    Min/max are logged; outlier gaps (jitter / a dropped sample) do not fail the check.
    """
    s_sec = _parse_interval_to_seconds(sample_interval)
    assert s_sec is not None, 'Invalid sample_interval: %s' % (sample_interval,)
    if tolerance < 1:
        min_gap = s_sec * (1 - tolerance)
        max_gap = s_sec * (1 + tolerance)
    else:
        min_gap = s_sec - tolerance
        max_gap = s_sec + tolerance

    results: Dict[str, Any] = {}
    failures: List[str] = []
    for model in models or []:
        series_key = '%s%s' % (series_key_prefix, model)
        try:
            series = telemetryCache.get_data(series_key)
        except KeyError:
            series = None

        model_result: Dict[str, Any] = {
            'model': model,
            'expected': {
                'sample_interval': s_sec,
                'tolerance': tolerance,
                'min_gap': min_gap,
                'max_gap': max_gap,
            },
        }
        if not isinstance(series, dict) or not series:
            model_result['result'] = 'fail'
            model_result['error'] = 'Missing or empty GNMI series for %s' % (model,)
            results[model] = model_result
            failures.append(model_result['error'])
            continue

        ts_keys: List[int] = []
        for key in series:
            try:
                ts_keys.append(int(key))
            except (TypeError, ValueError):
                continue
        ts_keys = sorted(set(ts_keys))
        if len(ts_keys) < 2:
            model_result['result'] = 'fail'
            model_result['error'] = '%s GNMI series has too few timestamps (%d)' % (model, len(ts_keys))
            results[model] = model_result
            failures.append(model_result['error'])
            continue

        with allure.step('Validate %s gNMI sample interval (median gap vs %ss)' % (model, s_sec)):
            ts_scale = 1_000_000_000 if max(ts_keys) > 1_000_000_000_000 else 1
            gaps = [(cur_ts - prev_ts) / ts_scale for prev_ts, cur_ts in zip(ts_keys[:-1], ts_keys[1:])]
            median_gap = statistics.median(gaps)
            logger.info(
                "[GNMI interval][%s] gaps count=%d min=%.1f median=%.1f avg=%.1f max=%.1f expected=%ss",
                model,
                len(gaps),
                min(gaps),
                median_gap,
                sum(gaps) / len(gaps),
                max(gaps),
                s_sec,
            )
            if median_gap < min_gap or median_gap > max_gap:
                model_result['result'] = 'fail'
                model_result['median_gap'] = median_gap
                model_result['gaps'] = gaps
                failures.append(
                    '%s median gap %.2fs outside expected %ss +/-%d%% (min=%.1fs max=%.1fs)'
                    % (model, median_gap, s_sec, int(tolerance * 100), min(gaps), max(gaps))
                )
            else:
                model_result['result'] = 'pass'
                model_result['median_gap'] = median_gap
                logger.info(
                    "[GNMI interval][%s] PASS: median=%.1fs within %ss +/-%d%% (min=%.1fs max=%.1fs count=%d)",
                    model,
                    median_gap,
                    s_sec,
                    int(tolerance * 100),
                    min(gaps),
                    max(gaps),
                    len(gaps),
                )
            results[model] = model_result

    try:
        existing = telemetryCache.get_data(cache_key)
    except KeyError:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing.update(results)
    telemetryCache.add_data(cache_key, existing)
    if failures:
        pytest.fail('GNMI per-timestamp interval violations: %s' % (failures,))


def validate_xpath_coverage_per_timestamp(
    *,
    model: str,
    attrs: Sequence[str],
    populate_kwargs: Optional[Dict[str, Any]] = None,
    series_key: Optional[str] = None,
    group_size: Optional[int] = None,
) -> None:
    """SSIM strict xpath coverage using cached ``gnmi-series-{model}``."""
    populate_kwargs = populate_kwargs or {}
    with allure.step('Resolve expected gNMI xpaths (model=%s)' % (model,)):
        expected_xpaths = resolve_model_xpaths(model, attrs, populate_kwargs)
        assert expected_xpaths, "No xpaths resolved for model '%s'" % (model,)
        allure.attach(
            '\n'.join(expected_xpaths),
            'expected_xpaths',
        )

    s_key = series_key or 'gnmi-series-%s' % (model,)
    with allure.step('Load %s from telemetry cache' % (s_key,)):
        try:
            gnmi_series = telemetryCache.get_data(s_key)
        except KeyError:
            gnmi_series = None
        assert isinstance(gnmi_series, dict) and gnmi_series, 'Missing or empty %s in telemetry cache' % (s_key,)
        allure.attach(
            'timestamps=%d populate_kwargs=%r attrs=%r'
            % (len(gnmi_series), populate_kwargs, list(attrs)),
            'gnmi_series_summary',
        )

    if isinstance(group_size, int) and group_size > 0:
        effective_group_size = group_size
    elif model == 'lldp':
        effective_group_size = 6
    elif model in ('bgp', 'interface'):
        effective_group_size = 2
    else:
        effective_group_size = 1

    def _normalize(xpath: str) -> str:
        return xpath[1:] if xpath.startswith('/') else xpath

    series_items = list(gnmi_series.items())
    failures: Dict[str, List[str]] = {}

    with allure.step(
        'Validate xpath coverage per timestamp (group_size=%d, %d timestamps)'
        % (effective_group_size, len(series_items))
    ):
        if effective_group_size <= 1:
            for ts, entries in series_items:
                present = set()
                for key, val in (entries or {}).items():
                    if isinstance(key, str):
                        present.add(key)
                        present.add(_normalize(key))
                    if isinstance(val, dict) and val.get('xpath'):
                        present.add(val['xpath'])
                        present.add(_normalize(val['xpath']))
                missing = [
                    xp for xp in expected_xpaths
                    if xp not in present and _normalize(xp) not in present
                ]
                if missing:
                    failures[str(ts)] = missing
        else:
            total = len(series_items)
            for i in range(total):
                start = max(0, i - (effective_group_size - 1))
                window = series_items[start: i + 1]
                union_present = set()
                for _, entries in window:
                    for key, val in (entries or {}).items():
                        if isinstance(key, str):
                            union_present.add(key)
                            union_present.add(_normalize(key))
                        if isinstance(val, dict) and val.get('xpath'):
                            union_present.add(val['xpath'])
                            union_present.add(_normalize(val['xpath']))
                missing = [
                    xp for xp in expected_xpaths
                    if xp not in union_present and _normalize(xp) not in union_present
                ]
                if missing:
                    failures[str(series_items[i][0])] = missing

        if failures:
            allure.attach(str(failures), 'xpath_coverage_failures')
            pytest.fail(
                'XPath coverage failures for %s (%d timestamps): %s'
                % (model, len(failures), failures)
            )
        allure.attach(
            'all %d timestamps satisfied %d xpaths (group_size=%d)'
            % (len(series_items), len(expected_xpaths), effective_group_size),
            'xpath_coverage_ok',
        )


# --- SSIM3 interface_top_level_subscription (strict bulk) ------------------

# SSIM3 ``collect_verify_bulk_subscription`` noise filter (``_NOISE_XPATH_SUBSTRINGS``):
# received xpaths matching these are dropped before the strict unexpected check because they
# are not stable across platforms/images or are gated behind disabled features.
_INTERFACE_NOISE_XPATH_SUBSTRINGS: Tuple[str, ...] = (
    'shared-buffer',
    'headroom-buffer',
    'integrated-circuit',
    'hold-time/state/counters/',
    'phy/link-down-information/state/',
)

# phy RS-FEC corrected-errors histogram bucket family (``rs-num-corr-err[upper-boundary=N]``).
# The exact bucket set is DUT-specific and discovered from NVUE before subscribing.
_INTERFACE_HISTOGRAM_XPATH_SUBSTRING: str = 'phy/histograms/state/rs-num-corr-err'


def _strip_leading_slash(xpath: str) -> str:
    return xpath[1:] if xpath.startswith('/') else xpath


def _filter_noise_component_xpaths(received_keys, noise_substrings):
    """SSIM3 ``_filter_noise_component_xpaths`` — drop platform-variant / feature-gated leaves.

    Applied to ``received`` for every subscription type (SSIM3's ``_NOISE_XPATH_SUBSTRINGS`` is a
    single shared tuple), so a noisy leaf counts toward neither the missing nor the unexpected check.
    """
    if not noise_substrings:
        return set(received_keys)
    return {xp for xp in received_keys if not any(n in xp for n in noise_substrings)}


def _filter_transient_process_pids(unexpected_keys):
    """SSIM3 ``_filter_transient_process_pids`` (verbatim) — system_top_level_subscription helper.

    The ``/system`` stream carries volatile per-key trees the modeled enumeration cannot predict:
    transient/restarted process PIDs and the not-yet-modeled systemd ``services`` tree. Split the
    unexpected set into ``(genuine, transient)`` so only genuine unexpected xpaths fail the test.
    This is test-case-specific (system) and is injected by ``validate_system_top_level_subscription``;
    the common validator stays model-agnostic.
    """
    transient = set()
    genuine = set()
    for key in unexpected_keys:
        if 'system/processes/process[pid=' in key:
            transient.add(key)
        elif 'system/services/service[name=' in key:
            transient.add(key)
        else:
            genuine.add(key)
    return genuine, transient


_PROCESS_PID_RE = re.compile(r'system/processes/process\[pid=(\d+)\]')

# SSIM3 ``handle_dynamic_process_pids`` — services known to restart mid-subscription. A new PID
# streamed for one of these is validated (its modeled leaves added to expected) rather than merely
# tolerated.
_DYNAMIC_SERVICES: Tuple[str, ...] = (
    'prometheus-node-exporter.service',
    'prometheus-sdk-stats.service',
)


def filter_old_process_pids(expected_keys, captured_pid_map, live_pids_fallback=None):
    """SSIM3 ``filter_old_process_pids`` parity — drop expected leaves of stale (dead) process PIDs.

    A discovered PID whose process exits/restarts before or during the subscription window streams
    none of its modeled leaves; without reconciliation all 8 would false-fail coverage. SSIM3 keeps
    only expected ``process[pid=X]`` leaves whose PID is in ``captured_subscription_pids`` — the
    independent OS snapshot taken *during* the window. When no capture is available we fall back to
    ``live_pids_fallback`` (PIDs actually seen in the stream) so the check degrades gracefully.
    """
    live_pids = {str(p) for p in _flatten_pid_map(captured_pid_map)} if captured_pid_map else set()
    if not live_pids and live_pids_fallback:
        live_pids = {str(p) for p in live_pids_fallback}

    kept = set()
    for xp in expected_keys:
        match = _PROCESS_PID_RE.search(xp)
        if match and live_pids and match.group(1) not in live_pids:
            continue  # stale/dead PID — not alive during the window (SSIM3 filter_old_process_pids)
        kept.add(xp)
    return kept


def handle_dynamic_process_pids(received_keys, expected_keys_base, captured_pid_map, current_pid_map):
    """SSIM3 ``handle_dynamic_process_pids`` parity — add restarted dynamic-service PIDs to expected.

    A service in ``_DYNAMIC_SERVICES`` (prometheus) can restart mid-window and stream a *new* PID.
    SSIM3 validates that new PID (rather than merely tolerating it) by generating its modeled leaves
    from an existing ``process[pid=N]`` pattern and adding them to the expected set — but only when
    the new PID belongs to a dynamic service in the current or captured PID map.
    """
    new_pids = set()
    for xp in received_keys:
        match = _PROCESS_PID_RE.search(xp)
        if not match:
            continue
        pid = match.group(1)
        if not any(('[pid=%s]' % pid) in exp for exp in expected_keys_base):
            new_pids.add(pid)
    if not new_pids:
        return expected_keys_base

    def _pid_in_dynamic_service(pid_str, pid_map):
        for service_name, pids in (pid_map or {}).items():
            if service_name in _DYNAMIC_SERVICES and int(pid_str) in pids:
                return True
        return False

    valid_new = {
        pid for pid in new_pids
        if _pid_in_dynamic_service(pid, current_pid_map) or _pid_in_dynamic_service(pid, captured_pid_map)
    }
    if not valid_new:
        return expected_keys_base

    patterns = {
        _PROCESS_PID_RE.sub('system/processes/process[pid={}]', exp)
        for exp in expected_keys_base if 'system/processes/process[pid=' in exp
    }
    updated = set(expected_keys_base)
    for pid in valid_new:
        for pattern in patterns:
            updated.add(pattern.format(pid))
    if updated != expected_keys_base:
        logger.info(
            'handle_dynamic_process_pids: added leaves for %d restarted dynamic-service PID(s): %s',
            len(valid_new), sorted(valid_new),
        )
    return updated


def _is_rate_xpath(xpath: str) -> bool:
    """SSIM ``skip_rate_metrics_first_timestamp`` rate-leaf heuristic (rates need two samples)."""
    return '/rates/state/' in xpath or xpath.endswith('-rate')


def discover_phy_histogram_boundary(dut, interface: str) -> List[int]:
    """SSIM3 ``get_phy_histogram_boundary`` parity via NVUE/OpenAPI (best-effort, [] on failure).

    Reads the RS-FEC corrected-errors histogram bucket ``upper-boundary`` values so the
    ``rs-num-corr-err[upper-boundary=N]`` xpaths match exactly what the DUT streams.
    """
    histogram_node = BaseComponent(
        Interface(None, port_name=interface).link.phy.health,
        path='/histogram',
    )
    data = _show_json(histogram_node, dut)

    buckets = data.get('rs-fec-corrected-errors') if isinstance(data, dict) else None
    if not isinstance(buckets, dict):
        buckets = data if isinstance(data, dict) else {}

    boundaries: List[int] = []
    for bucket_key, bucket in buckets.items():
        # SSIM3 reads each bucket's ``upper-boundary`` field; on platforms that do not
        # expose that field the bucket is keyed by its boundary index (the same value the
        # gNMI stream uses in ``rs-num-corr-err[upper-boundary=N]``), so fall back to the key.
        value: Any = None
        if isinstance(bucket, dict):
            value = bucket.get('upper-boundary', bucket.get('upper_boundary'))
        if value is None:
            value = bucket_key
        try:
            boundaries.append(int(str(value)))
        except (TypeError, ValueError):
            continue
    return sorted(set(boundaries))


def build_smoke_interface_expected_xpaths(
    dut,
    interface: str,
    *,
    lane: int = 5,
    auto_neg: bool = True,
    link_debounce: bool = False,
) -> Tuple[List[str], str]:
    """Full SSIM3 interface_xpath set + top_level for ``interface`` (ngts-adapted inputs)."""
    nvue_version = detect_nvue_python_pkg_version(dut)
    histogram_boundary = discover_phy_histogram_boundary(dut, interface)
    all_xpaths, top_level = build_interface_top_level_xpaths(
        interface,
        nvue_pkg_version=nvue_version,
        lane=lane,
        auto_neg=auto_neg,
        histogram_boundary=histogram_boundary,
        link_debounce=link_debounce,
    )
    logger.info(
        'SSIM3 interface top-level expected xpaths: %d (nvue=%s, histogram_buckets=%d)',
        len(all_xpaths), nvue_version, len(histogram_boundary),
    )
    return all_xpaths, top_level


# --- SSIM3 system_top_level_subscription dynamic discovery -----------------

_SYSTEM_CONTEXT_CACHE_KEY = 'gnmi-system-context'
# SSIM3 ``capture_pids_during_subscription`` snapshot ({service: [pids]}) taken *during* the gNMI
# collection window — the independent OS truth for ``filter_old_process_pids``/dynamic-PID handling.
_SYSTEM_CAPTURED_PIDS_CACHE_KEY = 'gnmi-system-captured-pids'


def discover_system_cpu_cores(dut) -> int:
    """SSIM ``numCores = int(uut.device.sudo('nproc'))`` parity (>=1 on failure)."""
    out = dut.run_cmd('nproc', validate=False, print_output=False)
    match = re.search(r'\d+', out or '')
    return int(match.group(0)) if match else 1


_LEGACY_MOUNT_POINTS: List[str] = ['/', '/var', '/run', '/run/lock', '/tmp', '/dev', '/dev/shm']
_MODERN_MOUNT_POINTS: List[str] = [
    '/', '/var', '/run', '/run/lock', '/tmp', '/var/tmp/nv-gnmi/gnoi_staging',
]
_MOUNT_POINT_XPATH_SUBSTRING = 'system/mount-points/mount-point['


def _first_json_object(out: Optional[str]):
    """Best-effort parse of the first JSON object or array in NVUE / tool output.

    NVUE ``-o json`` objects (possibly wrapped in SSH noise) go through
    ``OutputParsingTool.parse_json_str_to_dictionary``. That helper only carves ``{...}``
    from wrapped output, so a ``[...]`` blob (``smonctl -j``) falls back to a local slice.
    Parse failures are ignored: discovery treats missing JSON as "not present".
    """
    if not out or 'error:' in out.lower():
        return None
    parsed = OutputParsingTool.parse_json_str_to_dictionary(out)
    parsed.ignore_result()
    if parsed.result:
        return parsed.returned_value
    start = out.find('[')
    end = out.rfind(']')
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(out[start:end + 1])
    except (ValueError, json.JSONDecodeError):
        return None


def _show_json(node, dut, *, op_param: str = ""):
    """Best-effort domain-object show parsed as JSON. Missing nodes return None.

    Always ``ignore_result()`` so discovery cannot fail the test (e.g. SP6 has no
    fan node). Call ``.show(dut_engine=dut)`` on Platform/System/Interface nodes
    instead of ``dut.run_cmd('nv show ...')`` so OpenAPI is supported.
    """
    try:
        result = node.show(op_param=op_param, dut_engine=dut, if_returned_value=False)
    except Exception as exc:  # noqa: BLE001 - best-effort discovery
        path = node.get_resource_path() if hasattr(node, 'get_resource_path') else node
        logger.warning('Show failed for %s: %s', path, exc)
        return None
    result.ignore_result()
    raw = result.returned_value
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    return _first_json_object(str(raw))


def discover_system_mount_points(dut) -> List[str]:
    """SSIM3 ``get_return_mount_point_info`` parity — a FIXED, image-release-keyed mount set.

    The gNMI ``/system/mount-points`` set is NOT the live ``df`` output: SSIM3 returns a hard-coded
    list selected by the running partition's release — the legacy set for < 5.15, the modern set
    for >= 5.15 (``/mnt`` omitted on this image family; see ``_MODERN_MOUNT_POINTS``). The current
    partition's release is read from ``System().image``.
    """
    data = _show_json(System().image, dut)
    if not isinstance(data, dict):
        return _LEGACY_MOUNT_POINTS

    current = data.get(ImageConsts.CURRENT_IMG)
    if current is None:
        return _LEGACY_MOUNT_POINTS
    partition = data.get('%s%s' % (ImageConsts.PARTITION, current))
    if not isinstance(partition, dict):
        return _LEGACY_MOUNT_POINTS
    release = partition.get('release')
    if not isinstance(release, str):
        return _LEGACY_MOUNT_POINTS

    parts = release.split('.')
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return _LEGACY_MOUNT_POINTS
    return _MODERN_MOUNT_POINTS if (int(parts[0]), int(parts[1])) >= (5, 15) else _LEGACY_MOUNT_POINTS


def get_system_process_pid_map(dut) -> Dict[str, List[int]]:
    """SSIM3 ``get_system_process_pid`` parity — ``{service_name: [pids]}`` for active services.

    Mirrors SSIM3: (1) list the systemd units in the default telemetry unit-profile
    (``System().telemetry.software_stats.systemd.unit_profile`` / ``default``); (2) for each
    active unit read its cgroup ``cgroup.procs`` and collect every PID. Returns the
    service->PIDs mapping (needed by ``handle_dynamic_process_pids`` to test membership
    of dynamic services).
    """
    profile = SoftwareStatsUnitProfile(
        System().telemetry.software_stats.systemd.unit_profile,
        'default',
    )
    data = _show_json(profile, dut)
    units = list((data.get('unit') or {}).keys()) if isinstance(data, dict) else []
    if not units:
        logger.warning('No systemd units found in default telemetry unit-profile; no process PIDs.')
        return {}

    # Batch the per-unit cgroup lookups into one shell round-trip (SSIM3 loops per unit): for each
    # active unit emit a ``UNIT:<name>`` marker then its cgroup.procs, so we can rebuild the map.
    unit_args = ' '.join(shlex.quote(u) for u in units)
    script = (
        'for u in %s; do '
        'systemctl is-active --quiet "$u" || continue; '
        'cg=$(systemctl show -p ControlGroup --value "$u" 2>/dev/null); '
        '[ -n "$cg" ] || continue; '
        'echo "UNIT:$u"; '
        'cat "/sys/fs/cgroup${cg}/cgroup.procs" 2>/dev/null; '
        'done'
    ) % (unit_args,)
    procs_out = dut.run_cmd("sudo bash -c %s" % shlex.quote(script), validate=False, print_output=False)

    result: Dict[str, List[int]] = {}
    current_unit: Optional[str] = None
    for line in (procs_out or '').splitlines():
        token = line.strip()
        if token.startswith('UNIT:'):
            current_unit = token[len('UNIT:'):]
            continue
        if current_unit and token.isdigit():
            value = int(token)
            if value > 0:
                result.setdefault(current_unit, []).append(value)
    return result


def _flatten_pid_map(pid_map: Dict[str, List[int]]) -> List[int]:
    pids: set = set()
    for values in (pid_map or {}).values():
        for value in values:
            if isinstance(value, int) and value > 0:
                pids.add(value)
    return sorted(pids)


def discover_system_process_pids(dut) -> List[int]:
    """Flat, de-duplicated active-service PID list for the expected-xpath builder.

    SSIM3's ``system_top_level_subscription`` flattens ``get_system_process_pid``'s service->PIDs
    mapping into ``all_pids``; this returns that flattened list.
    """
    return _flatten_pid_map(get_system_process_pid_map(dut))


def discover_system_top_level_context(dut) -> Dict[str, Any]:
    """Discover the dynamic keys (cores/mount points/pids/nvue) for the system expected set."""
    context = {
        'num_cores': discover_system_cpu_cores(dut),
        'mount_points': discover_system_mount_points(dut),
        'pids': discover_system_process_pids(dut),
        'nvue_version': detect_nvue_python_pkg_version(dut),
    }
    logger.info(
        'SSIM3 system top-level context: cores=%s mount_points=%d pids=%d nvue=%s',
        context['num_cores'], len(context['mount_points']), len(context['pids']),
        context['nvue_version'],
    )
    return context


def build_smoke_system_expected_xpaths(dut) -> Tuple[List[str], str]:
    """Full SSIM3 system_xpath set + top_level for ``/system`` (ngts-adapted discovery).

    Reuses the context discovered at collection time (cached under ``gnmi-system-context`` by
    ``build_smoke_gnmi_jobs``) so the expected set is consistent with the subscription
    window; falls back to live discovery when the cache is absent.
    """
    context: Optional[Dict[str, Any]] = None
    try:
        cached = telemetryCache.get_data(_SYSTEM_CONTEXT_CACHE_KEY)
    except KeyError:
        cached = None
    if isinstance(cached, dict):
        context = cached
    if context is None:
        context = discover_system_top_level_context(dut)

    all_xpaths, top_level = build_system_top_level_xpaths(
        context.get('num_cores', 1),
        context.get('mount_points', ['/']),
        context.get('pids', []),
        nvue_pkg_version=context.get('nvue_version'),
    )
    logger.info(
        'SSIM3 system top-level expected xpaths: %d (nvue=%s, cores=%s, mounts=%d, pids=%d)',
        len(all_xpaths), context.get('nvue_version'), context.get('num_cores'),
        len(context.get('mount_points', [])), len(context.get('pids', [])),
    )
    return all_xpaths, top_level


# --- SSIM3 components_top_level_subscription dynamic discovery -------------

_COMPONENT_CONTEXT_CACHE_KEY = 'gnmi-component-context'

# SSIM3 ``_NOISE_XPATH_SUBSTRINGS`` — platform-variant / feature-gated leaves dropped from the
# received stream so they count toward neither the missing nor the unexpected check. ``/components``
# only really needs ``integrated-circuit`` (also dropped from expected), but the full shared tuple
# is used for parity.
_COMPONENT_NOISE_XPATH_SUBSTRINGS: Tuple[str, ...] = (
    'shared-buffer', 'headroom-buffer', 'integrated-circuit',
    'hold-time/state/counters/', 'phy/link-down-information/state/',
)

_BAD_ABSENT_STATES = {'absent', 'bad'}
# Vendor-PN prefix -> transceiver_type. Discovery and the cable-type fallback share this map so a
# prefix cannot be "active" and still classify as ``unknown`` (the 980 gap). NVUE ``cable-type``
# still wins when present; ``980`` defaults to copper only when cable-type is absent.
_TRANSCEIVER_PN_TYPE = {
    'MCP': 'copper',
    '980': 'copper',
    'MMS': 'fiber',
    'MFM': 'fiber',
    'MFA': 'fiber',
}
_ACTIVE_TRANSCEIVER_PREFIXES = tuple(_TRANSCEIVER_PN_TYPE)
_BREAKOUT_PORT_RE = re.compile(r'^(swp\d+)s(\d+)$')

# SSIM3 keeps ``transceiver/state/ethernet-pmd`` in the expected set for every copper/fiber module
# (see ComponentsXPaths._add_transceiver_common). On some modules the platform simply has no PMD /
# 802.3 compliance value to stream — e.g. passive-copper QSFP28 DACs, whose NVUE transceiver model
# exposes no ``ethernet-pmd`` field at all. SSIM3 does not filter this and its own ``test_09`` fails
# on such HW (a known device-side gap that is not addressed). We keep the leaf expected (parity) but list it
# here as a *known device gap*: when it is persistently absent for a discovered copper transceiver
# it is logged and tolerated instead of hard-failing coverage — never silently dropped from the
# expected model.
_TRANSCEIVER_ETHERNET_PMD_SUFFIX = '/transceiver/state/ethernet-pmd'


def _component_known_device_gap_xpaths(context: Optional[Dict[str, Any]]) -> set:
    """Per-DUT known-gap xpaths tolerated (logged, not failed) when persistently missing.

    Currently: ``ethernet-pmd`` for each discovered *copper* transceiver (passive-copper DACs do not
    stream it; SSIM3 parity keeps it expected). Scoped to the exact discovered copper modules so a
    genuinely-missing ``ethernet-pmd`` on a fiber module is still caught.
    """
    gaps: set = set()
    if not isinstance(context, dict):
        return gaps
    for descriptor in (context.get('components_by_type', {}) or {}).get(ComponentType.TRANSCEIVER, []) or []:
        if not isinstance(descriptor, dict):
            continue
        if descriptor.get('transceiver_type') != 'copper':
            continue
        name = descriptor.get('name')
        component_name = name.get('component_name') if isinstance(name, dict) else name
        if component_name:
            gaps.add(_strip_leading_slash(
                'components/component[name=%s]%s' % (component_name, _TRANSCEIVER_ETHERNET_PMD_SUFFIX)
            ))
    return gaps


def _discover_psu(dut, bad_absent: Dict[str, Tuple[str, ...]]) -> List[str]:
    data = _show_json(Platform().environment.psu, dut)
    names: List[str] = []
    if isinstance(data, dict):
        for name, info in data.items():
            state = str((info or {}).get('state', '')).lower() if isinstance(info, dict) else ''
            if state in _BAD_ABSENT_STATES:
                # BAD/ABSENT PSU still streams name/description/oper-status *and* state/type.
                bad_absent[name] = PSU_BAD_ABSENT_LEAVES
            else:
                names.append(name)
    return names


def _discover_fan(dut) -> List[str]:
    data = _show_json(Platform().environment.fan, dut)
    if not isinstance(data, dict):
        # SP6 (spectrum6) has no 'fan' environment node; the command errors -> skip FAN gracefully.
        return []
    return [
        name for name, info in data.items()
        if isinstance(info, dict) and str(info.get('state', '')).lower() not in _BAD_ABSENT_STATES
    ]


# gNMI / smonctl temperature component names (``TempN``, ``PSU{n}Temp{m}``). NVUE inventory
# keys on many mlx platforms differ (``Asic-Temp-Sensor``, ``PSU1-Temp-Sensor``, …).
_OC_BOARD_TEMP_RE = re.compile(r'^Temp(\d+)$', re.IGNORECASE)
_OC_PSU_TEMP_RE = re.compile(r'^PSU(\d+)Temp(\d+)$', re.IGNORECASE)
_NVUE_PSU_TEMP_RE = re.compile(
    r'PSU[-_]?(\d+)[-_/\s]?Temp(?:erature)?(?:[-_]?Sensor)?(\d+)?',
    re.IGNORECASE,
)
_TEMP_SENSOR_INFO_KEYS = ('state', 'current', 'max', 'min', 'description')


def _looks_like_temp_sensor_info(info: Any) -> bool:
    return isinstance(info, dict) and any(key in info for key in _TEMP_SENSOR_INFO_KEYS)


def _unwrap_temperature_show(data: dict) -> dict:
    """Return the sensor-name -> info map, peeling a single OpenAPI/CLI wrapper if present."""
    sensors = {name: info for name, info in data.items() if _looks_like_temp_sensor_info(info)}
    if sensors:
        return sensors
    nested = [value for value in data.values() if isinstance(value, dict)]
    if len(nested) == 1:
        return {
            name: info for name, info in nested[0].items() if _looks_like_temp_sensor_info(info)
        }
    return data


def _iter_temperature_entries(data) -> Iterable[Tuple[str, dict]]:
    """Yield ``(nvue_name, info)`` from a temperature show dict or smonctl-shaped list."""
    if isinstance(data, dict):
        for name, info in _unwrap_temperature_show(data).items():
            yield str(name), info if isinstance(info, dict) else {}
        return
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get('name'):
                yield str(item['name']), item


def _gnmi_temp_name_from_nvue(nvue_name: str) -> Optional[str]:
    """Map an NVUE temperature key to the OpenConfig name when it is already OC or a PSU temp.

    Board sensors whose NVUE key is *not* ``TempN`` (e.g. ``Asic-Temp-Sensor``) return ``None``
    so the caller can number them ``Temp1..N``. PSU temps become ``PSU{n}Temp{m}``.
    """
    match = _OC_BOARD_TEMP_RE.match(nvue_name)
    if match:
        return 'Temp%s' % (match.group(1),)
    match = _OC_PSU_TEMP_RE.match(nvue_name)
    if match:
        return 'PSU%sTemp%s' % (match.group(1), match.group(2))
    match = _NVUE_PSU_TEMP_RE.search(nvue_name)
    if match:
        return 'PSU%sTemp%s' % (match.group(1), match.group(2) or '1')
    return None


def _discover_temp(dut, bad_absent: Dict[str, Tuple[str, ...]]) -> List[Dict[str, str]]:
    """SSIM3 TEMP list as ``{gNMI-name: description}``, from ``Platform().environment.temperature``.

    gNMI streams smonctl names (``Temp1``, ``PSU1Temp1``). NVUE show keys are often the
    inventory labels (``Asic-Temp-Sensor``, ``PSU1-Temp-Sensor``). Using the NVUE key as
    the component name makes every TEMP leaf look unexpected. Map onto the OC names and
    keep BAD/ABSENT PSU temps in ``bad_absent`` (basic leaves only — they do not stream
    the temperature subtree).
    """
    data = _show_json(Platform().environment.temperature, dut)
    entries = list(_iter_temperature_entries(data))
    temps: List[Dict[str, str]] = []
    pending_board: List[Tuple[str, dict]] = []
    used_board_nums: set = set()
    resolved: List[Tuple[str, str, dict]] = []  # (oc_name, nvue_name, info)

    for nvue_name, info in entries:
        oc_name = _gnmi_temp_name_from_nvue(nvue_name)
        if oc_name is None:
            pending_board.append((nvue_name, info))
            continue
        board = _OC_BOARD_TEMP_RE.match(oc_name)
        if board:
            used_board_nums.add(int(board.group(1)))
        resolved.append((oc_name, nvue_name, info))

    next_n = 1
    for nvue_name, info in pending_board:
        while next_n in used_board_nums:
            next_n += 1
        oc_name = 'Temp%d' % (next_n,)
        used_board_nums.add(next_n)
        next_n += 1
        resolved.append((oc_name, nvue_name, info))

    for oc_name, nvue_name, info in resolved:
        state = str(info.get('state', '')).lower()
        logger.info('TEMP discovery: NVUE %s -> gNMI %s state=%s', nvue_name, oc_name, state or '?')
        if state in _BAD_ABSENT_STATES:
            # BAD/ABSENT TEMP streams only basic leaves (no temperature subtree).
            bad_absent[oc_name] = BASIC_LEAVES
            continue
        description = str(info.get('description', nvue_name)).replace(' ', '-')
        temps.append({oc_name: description})
    return temps


def _transceiver_type_from_pn(vendor_pn: str) -> str:
    """Map an active vendor-PN prefix to copper/fiber; ``unknown`` if the PN is not in the map."""
    for prefix, xcvr_type in _TRANSCEIVER_PN_TYPE.items():
        if vendor_pn.startswith(prefix):
            return xcvr_type
    return 'unknown'


def _transceiver_type_from_entry(entry: dict, vendor_pn: str) -> str:
    """Classify copper/fiber from the bulk transceiver record; PN map is the fallback."""
    cable_type = str(entry.get(PlatformConsts.TRANSCEIVER_CABLE_TYPE, '')).strip()
    if cable_type.lower() == PlatformConsts.TRANSCEIVER_CABLE_OPTICAL_MODULE.lower():
        return 'fiber'
    if cable_type.lower() == PlatformConsts.TRANSCEIVER_CABLE_COPPER_CABLE.lower():
        return 'copper'
    return _transceiver_type_from_pn(vendor_pn)


def _transceiver_firmware_present_from_entry(entry: dict) -> bool:
    """True when the bulk transceiver record reports a non-placeholder module firmware."""
    fw = ''
    for key in ('fw-version', 'module-fw-version'):
        value = entry.get(key)
        if value is not None and str(value).strip():
            fw = str(value).strip()
            break
    firmware = entry.get('firmware')
    if not fw and isinstance(firmware, dict):
        fw = str(
            firmware.get(PlatformConsts.FW_ACTUAL) or firmware.get('module-fw-version') or ''
        ).strip()
    return bool(fw) and fw != '0.0.0'


def _discover_transceivers(dut) -> List[Dict[str, Any]]:
    """SSIM3 ``return_component_list(TRANSCEIVER)`` — active (vendor-pn) + inactive (down swp) modules.

    Cable type and firmware come from the bulk ``Platform().transceiver`` dict so a
    fully-cabled switch does not add a per-port SSH round trip for each module.
    """
    transceivers: List[Dict[str, Any]] = []
    xcvr_info = _show_json(Platform().transceiver, dut)
    xcvr_info = xcvr_info if isinstance(xcvr_info, dict) else {}

    for swp, value in xcvr_info.items():
        entry = value if isinstance(value, dict) else {}
        vendor_pn = str(entry.get(PlatformConsts.TRANSCEIVER_VENDOR_PN, ''))
        if not vendor_pn.startswith(_ACTIVE_TRANSCEIVER_PREFIXES):
            continue
        component_name_val = swp.replace('swp', 'transceiver')
        transceivers.append({
            'name': {'component_name': component_name_val, 'nvue_name': swp},
            'transceiver_type': _transceiver_type_from_entry(entry, vendor_pn),
            'firmware-present': _transceiver_firmware_present_from_entry(entry),
        })

    down = _show_json(Interface(None), dut, op_param='--view=down')
    down = down if isinstance(down, dict) else {}
    processed_parents: set = set()
    for interface_name, interface_data in down.items():
        if not isinstance(interface_data, dict) or interface_data.get('type') != 'swp':
            continue
        if interface_name in xcvr_info:
            continue
        breakout = _BREAKOUT_PORT_RE.match(interface_name)
        if breakout:
            parent = breakout.group(1)
            if parent in xcvr_info or parent in processed_parents:
                continue
            processed_parents.add(parent)
            component_name_val = parent.replace('swp', 'transceiver')
        else:
            component_name_val = interface_name.replace('swp', 'transceiver')
        transceivers.append({
            'name': {'component_name': component_name_val, 'nvue_name': interface_name},
            'transceiver_type': 'inactive',
            'firmware-present': False,
        })
    return transceivers


def _discover_asic(dut) -> List[Dict[str, str]]:
    data = _show_json(Platform().firmware, dut)
    if not isinstance(data, dict):
        return []
    return [{'component_name': name} for name in data if str(name).startswith('Spectrum')]


_STORAGE_NAME_RE = re.compile(r'^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|hd[a-z]+|xvd[a-z]+)$')


def _discover_storage(dut) -> List[Dict[str, Any]]:
    """Discover block devices and whether they expose ATA-IDENTIFY counters.

    ``rotation-rate-rpm`` / ``write-cache`` / ``write-cache-enabled`` are only streamed for
    real SATA/ATA disks. USB sticks (and NVMe) do not, so each descriptor carries
    ``ata_identify`` and the xpath builder includes those three leaves only when True.
    """
    out = dut.run_cmd(
        'lsblk -d -o NAME,TRAN,RM -n 2>/dev/null',
        validate=False,
        print_output=False,
    )
    devices: List[Dict[str, Any]] = []
    for line in (out or '').splitlines():
        parts = line.split()
        if not parts:
            continue
        name = parts[0].strip()
        if not _STORAGE_NAME_RE.match(name):
            continue
        tran = parts[1].strip().lower() if len(parts) > 1 else ''
        removable = parts[2].strip() if len(parts) > 2 else '0'
        # USB / removable mass-storage is not ATA; NVMe is gated later by name prefix.
        ata_identify = tran in ('sata', 'ata') and tran != 'usb' and removable != '1'
        devices.append({'name': name, 'ata_identify': ata_identify, 'tran': tran})
        logger.info(
            'STORAGE discovery: %s tran=%s rm=%s ata_identify=%s',
            name, tran or '?', removable, ata_identify,
        )
    return devices


def _discover_leakage(dut) -> List[str]:
    data = _show_json(Platform().environment.leakage, dut)
    if isinstance(data, dict):
        return [name for name in data if name]
    if isinstance(data, list):
        return [item['name'] for item in data if isinstance(item, dict) and item.get('name')]
    return []


def discover_components_top_level_context(dut) -> Dict[str, Any]:
    """Discover the per-type component set (SSIM3 ``return_component_list``) for the expected xpaths."""
    bad_absent: Dict[str, Tuple[str, ...]] = {}
    components_by_type: Dict[str, Any] = {
        ComponentType.PSU: _discover_psu(dut, bad_absent),
        ComponentType.FAN: _discover_fan(dut),
        ComponentType.TEMP: _discover_temp(dut, bad_absent),
        ComponentType.TRANSCEIVER: _discover_transceivers(dut),
        ComponentType.ASIC: _discover_asic(dut),
        ComponentType.CHASSIS: [{'component_name': 'CHASSIS'}],
        ComponentType.STORAGE: _discover_storage(dut),
        ComponentType.CPU: ['CPU'],
        ComponentType.LEAKAGE: _discover_leakage(dut),
    }
    context = {
        'components_by_type': components_by_type,
        'bad_absent': bad_absent,
        'nvue_version': detect_nvue_python_pkg_version(dut),
    }
    logger.info(
        'SSIM3 components top-level context: %s (bad/absent=%d, nvue=%s)',
        {t: len(components_by_type.get(t, [])) for t in COMPONENT_TYPES},
        len(bad_absent), context['nvue_version'],
    )
    return context


def build_smoke_component_expected_xpaths(dut) -> Tuple[List[str], str]:
    """Full SSIM3 components_xpath set + top_level for ``/components`` (ngts-adapted discovery).

    Reuses the context discovered at collection time (cached under ``gnmi-component-context`` by
    ``build_smoke_gnmi_jobs``) so the expected set is consistent with the subscription window;
    falls back to live discovery when the cache is absent.
    """
    context: Optional[Dict[str, Any]] = None
    try:
        cached = telemetryCache.get_data(_COMPONENT_CONTEXT_CACHE_KEY)
    except KeyError:
        cached = None
    if isinstance(cached, dict):
        context = cached
    if context is None:
        context = discover_components_top_level_context(dut)

    all_xpaths, top_level = build_components_top_level_xpaths(
        context.get('components_by_type', {}),
        bad_absent_components=context.get('bad_absent') or {},
        nvue_pkg_version=context.get('nvue_version'),
    )
    logger.info(
        'SSIM3 components top-level expected xpaths: %d (nvue=%s, bad/absent=%d)',
        len(all_xpaths), context.get('nvue_version'), len(context.get('bad_absent', [])),
    )
    return all_xpaths, top_level


def validate_top_level_subscription(
    *,
    expected_xpaths: Sequence[str],
    series_key: str,
    model_label: str = 'top-level',
    noise_substrings: Tuple[str, ...] = _INTERFACE_NOISE_XPATH_SUBSTRINGS,
    skip_rate_metrics_first_timestamp: bool = False,
    unexpected_filter: Optional[Callable[[set], set]] = None,
    missing_filter: Optional[Callable[[set, set], set]] = None,
    expected_augmenter: Optional[Callable[[set, set], set]] = None,
) -> None:
    """SSIM3 ``collect_verify_bulk_subscription`` per-timestamp verification — common to all tests.

    Model-agnostic port of SSIM3's bulk verifier: it knows nothing about interface/system/etc.
    Callers pass the already-resolved ``expected_xpaths`` and the cached ``gnmi-series-{model}``
    (parsed to ``{timestamp: {xpath: entry}}``). Every test case runs the same two checks:

    * ``_filter_noise_component_xpaths`` — ``noise_substrings`` are dropped from ``received`` up
      front, so a noisy leaf counts toward neither check (SSIM3 uses one shared tuple for all);
    * ``Check 1`` (coverage): an expected xpath is a *missing* failure only when absent from
      **every** update (intersection of per-update missing sets);
    * ``Check 2`` (strict): any received xpath not in the expected set is an unexpected failure;
    * ``skip_rate_metrics_first_timestamp``: rate leaves (which need two samples) are not counted
      missing on the first update.

    Test-case-specific tolerances are injected, not hard-coded here:

    * ``unexpected_filter`` — SSIM3's ``is_system_subscription`` hook. Given the per-update
      unexpected set it returns the subset that should still fail (e.g. ``/system`` peels transient
      process/service XPaths via ``_filter_transient_process_pids``). ``None`` = strict.
    * ``missing_filter`` — SSIM3's ``filter_old_process_pids`` hook. Given
      ``(persistent_missing, received_union)`` it returns the subset that should still fail, letting
      a caller reconcile the expected set against what actually streamed (e.g. ``/system`` drops
      stale/dead process PIDs that exited during the window). ``None`` = fail on all missing.
    * ``expected_augmenter`` — SSIM3's ``handle_dynamic_process_pids`` hook. Given
      ``(expected_norm, received)`` for one update it returns the expected set to validate against
      for that update, letting a caller add keys that only appear at runtime (e.g. ``/system`` adds
      leaves for a restarted dynamic-service PID so they are *required*, not merely tolerated).
    """
    assert expected_xpaths, 'No expected %s xpaths resolved (SSIM3 xpath set empty)' % (model_label,)
    expected_norm = {_strip_leading_slash(xp) for xp in expected_xpaths}
    noise = tuple(_strip_leading_slash(n) for n in noise_substrings)

    rate_expected = (
        {xp for xp in expected_norm if _is_rate_xpath(xp)}
        if skip_rate_metrics_first_timestamp else set()
    )

    with allure.step('Load %s from telemetry cache' % (series_key,)):
        try:
            series = telemetryCache.get_data(series_key)
        except KeyError:
            series = None
        assert isinstance(series, dict) and series, 'Missing or empty %s in telemetry cache' % (series_key,)
        allure.attach(
            'timestamps=%d expected_xpaths=%d' % (len(series), len(expected_norm)),
            '%s_top_level_summary' % (model_label,),
        )

    series_items = sorted(series.items(), key=lambda kv: str(kv[0]))
    persistent_missing: Optional[set] = None
    unexpected_failures: Dict[str, List[str]] = {}
    received_union: set = set()

    with allure.step(
        'Per-timestamp validation (%d timestamps, %d expected xpaths)'
        % (len(series_items), len(expected_norm))
    ):
        for idx, (ts, entries) in enumerate(series_items):
            received = set()
            for key, val in (entries or {}).items():
                if isinstance(key, str):
                    received.add(_strip_leading_slash(key))
                if isinstance(val, dict) and val.get('xpath'):
                    received.add(_strip_leading_slash(val['xpath']))
            received = _filter_noise_component_xpaths(received, noise)
            received_union |= received

            # SSIM3 handle_dynamic_process_pids: per-update expected set may gain runtime-only keys
            # (e.g. a restarted dynamic-service PID) so they are required rather than just tolerated.
            expected_ts = expected_norm
            if expected_augmenter is not None:
                expected_ts = expected_augmenter(expected_norm, received)

            # Check 1: coverage — fail only for xpaths missing from EVERY update.
            missing = expected_ts - received
            if skip_rate_metrics_first_timestamp and idx == 0:
                rate_ts = rate_expected if expected_augmenter is None else {
                    xp for xp in expected_ts if _is_rate_xpath(xp)
                }
                missing = missing - rate_ts
            persistent_missing = missing if persistent_missing is None else (persistent_missing & missing)

            # Check 2: strict — every received xpath must be expected, minus caller tolerances.
            unexpected = received - expected_ts
            if unexpected_filter is not None:
                unexpected = unexpected_filter(unexpected)
            if unexpected:
                unexpected_failures[str(ts)] = sorted(unexpected)

        # SSIM3 filter_old_process_pids: reconcile persistently-missing xpaths against what actually
        # streamed (drops stale keys whose parent exited mid-window) before failing coverage.
        if persistent_missing and missing_filter is not None:
            persistent_missing = missing_filter(persistent_missing, received_union)

        if unexpected_failures:
            allure.attach(str(unexpected_failures), '%s_unexpected_xpaths' % (model_label,))
            pytest.fail(
                '%s top-level subscription received UNEXPECTED xpaths in %d timestamp(s): %s'
                % (model_label.capitalize(), len(unexpected_failures), unexpected_failures)
            )

        if persistent_missing:
            allure.attach('\n'.join(sorted(persistent_missing)), '%s_missing_from_all_updates' % (model_label,))
            pytest.fail(
                '%s top-level subscription MISSING %d expected xpath(s) from ALL %d update(s): %s'
                % (model_label.capitalize(), len(persistent_missing), len(series_items), sorted(persistent_missing))
            )
        allure.attach(
            'all %d expected xpaths covered across %d timestamps (0 persistently missing, 0 unexpected)'
            % (len(expected_norm), len(series_items)),
            '%s_top_level_ok' % (model_label,),
        )


def validate_interface_top_level_subscription(
    *,
    expected_xpaths: Sequence[str],
    series_key: str = 'gnmi-series-interface',
    skip_rate_metrics_first_timestamp: bool = False,
) -> None:
    """SSIM3 interface ``collect_verify_bulk_subscription`` (thin wrapper of the common core).

    Interface streams a stable, fully modeled xpath set, so the strict unexpected check runs with
    no tolerance (``unexpected_filter=None``). The one interface-specific concern lives here, not
    in the common core: SSIM3 derives the exact phy-histogram bucket set from the DUT before
    subscribing; when ngts cannot enumerate those buckets (no ``rs-num-corr-err`` xpath in the
    expected set) the streamed buckets are added to the received-noise filter instead of failing
    as unexpected.
    """
    noise_substrings = _INTERFACE_NOISE_XPATH_SUBSTRINGS
    if not any(_INTERFACE_HISTOGRAM_XPATH_SUBSTRING in _strip_leading_slash(xp) for xp in expected_xpaths):
        logger.warning(
            'No histogram buckets in expected interface set; treating streamed %s buckets as noise '
            '(NVUE histogram boundary not discoverable).',
            _INTERFACE_HISTOGRAM_XPATH_SUBSTRING,
        )
        noise_substrings = noise_substrings + (_INTERFACE_HISTOGRAM_XPATH_SUBSTRING,)

    validate_top_level_subscription(
        expected_xpaths=expected_xpaths,
        series_key=series_key,
        model_label='interface',
        noise_substrings=noise_substrings,
        skip_rate_metrics_first_timestamp=skip_rate_metrics_first_timestamp,
    )


def validate_system_top_level_subscription(
    *,
    expected_xpaths: Sequence[str],
    dut=None,
    series_key: str = 'gnmi-series-system',
    skip_rate_metrics_first_timestamp: bool = True,
) -> None:
    """SSIM3 system ``collect_verify_bulk_subscription`` (thin wrapper of the common core).

    SSIM3 ``system_top_level_subscription`` calls the shared bulk verifier with
    ``skip_rate_metrics_first_timestamp=True`` and keeps the strict unexpected check ON. All three
    system-specific ``is_system_subscription`` behaviors live here (the common core stays generic):

    * ``_filter_transient_process_pids`` (unexpected side) — peels transient/restarted process PIDs
      and the not-yet-modeled systemd ``services`` tree from the unexpected set;
    * extra live ``/system/mount-points`` leaves (unexpected side) — the expected mount set is a
      fixed release-keyed baseline (``/mnt`` is not required on this image); extras are tolerated;
    * ``filter_old_process_pids`` (missing side) — drops persistently-missing leaves of PIDs that
      were not alive during the window, judged against the ``capture_pids_during_subscription``
      snapshot cached at collection time (independent OS truth), or the received stream as fallback;
    * ``handle_dynamic_process_pids`` (expected side) — adds leaves for a restarted dynamic-service
      PID so they are validated, not merely tolerated.

    ``dut`` (when supplied) is queried for the *current* ``{service_name: [pid, ...]}`` map via
    ``get_system_process_pid_map`` and passed to ``handle_dynamic_process_pids`` as
    ``current_pid_map``. This is SSIM3 parity for its "query current PIDs" step: it lets a
    dynamic-service (prometheus) restart that spawns a new PID *after* the during-window capture
    snapshot still be recognized and validated, rather than silently tolerated.
    """
    captured_pid_map: Dict[str, List[int]] = {}
    try:
        cached_pids = telemetryCache.get_data(_SYSTEM_CAPTURED_PIDS_CACHE_KEY)
    except KeyError:
        cached_pids = None
    if isinstance(cached_pids, dict):
        captured_pid_map = cached_pids

    def _system_unexpected_filter(unexpected: set) -> set:
        genuine, transient = _filter_transient_process_pids(unexpected)
        if transient:
            logger.info(
                'Ignoring %d unexpected system xpath(s) from transient processes/services: %s',
                len(transient), sorted(transient),
            )
        extra_mounts = {
            xp for xp in genuine if _MOUNT_POINT_XPATH_SUBSTRING in xp
        }
        if extra_mounts:
            # Expected mounts are a fixed release-keyed baseline, not live df. Labs may
            # stream extra mounts (e.g. ``/mnt``); require the baseline, tolerate extras.
            # Scoped to this system top-level validator only.
            logger.info(
                'Ignoring %d unexpected live mount-point xpath(s): %s',
                len(extra_mounts), sorted(extra_mounts),
            )
            genuine = genuine - extra_mounts
        return genuine

    def _system_missing_filter(persistent_missing: set, received_union: set) -> set:
        received_pids = set()
        for xp in received_union:
            match = _PROCESS_PID_RE.search(xp)
            if match:
                received_pids.add(match.group(1))
        still_failing = filter_old_process_pids(
            persistent_missing, captured_pid_map, live_pids_fallback=received_pids
        )
        dropped = persistent_missing - still_failing
        if dropped:
            logger.info(
                'Ignoring %d missing system xpath(s) from stale/dead process PIDs '
                '(not alive during window, SSIM3 filter_old_process_pids): %s',
                len(dropped), sorted(dropped),
            )
        return still_failing

    current_pid_map_cache: Dict[str, List[int]] = {}
    current_pid_map_queried = {'done': False}

    def _system_expected_augmenter(expected_norm: set, received: set) -> set:
        # Query the current (validation-time) PID map lazily and once: only needed when the stream
        # carried a not-yet-expected PID, and a single DUT round-trip suffices for all augmenter runs.
        if dut is not None and not current_pid_map_queried['done']:
            current_pid_map_queried['done'] = True
            try:
                fetched = get_system_process_pid_map(dut)
                if isinstance(fetched, dict):
                    current_pid_map_cache.update(fetched)
            except Exception as exc:  # discovery is best-effort; fall back to the captured snapshot
                logger.warning(
                    'Could not query current system PID map for dynamic-PID validation (%s); '
                    'relying on the during-window capture snapshot only.', exc,
                )
        return handle_dynamic_process_pids(
            received, expected_norm, captured_pid_map, current_pid_map_cache
        )

    validate_top_level_subscription(
        expected_xpaths=expected_xpaths,
        series_key=series_key,
        model_label='system',
        noise_substrings=_INTERFACE_NOISE_XPATH_SUBSTRINGS,
        skip_rate_metrics_first_timestamp=skip_rate_metrics_first_timestamp,
        unexpected_filter=_system_unexpected_filter,
        missing_filter=_system_missing_filter,
        expected_augmenter=_system_expected_augmenter,
    )


def discover_asic_model(dut) -> Optional[str]:
    """Return the DUT ASIC model reported by NVUE/OpenAPI."""
    payload = _show_json(Platform(), dut)
    if not isinstance(payload, dict):
        return None
    asic_model = payload.get('asic-model')
    return str(asic_model) if asic_model else None


def validate_qos_prefix_subscription(
    prefixes: Sequence[str],
    *,
    series_key: str = 'gnmi-series-qos-prefix',
    asic_model: Optional[str] = None,
) -> None:
    """Verify that every subscribed QoS prefix returned itself or descendants.

    Unlike top-level leaf validation, this only checks prefix coverage.
    Buffer-occupancy and other descendant leaves count as coverage.
    """
    try:
        series = telemetryCache.get_data(series_key)
    except KeyError:
        series = None
    assert isinstance(series, dict) and series, (
        'Missing or empty %s in telemetry cache' % (series_key,)
    )

    expected = filter_unsupported_qos_xpaths(
        (
            _strip_leading_slash(str(p)).rstrip('/')
            for p in prefixes
            if p
        ),
        asic_model=asic_model,
    )
    observed = set()
    for entries in series.values():
        if not isinstance(entries, dict):
            continue
        for xpath, entry in entries.items():
            observed.add(_strip_leading_slash(str(xpath)).rstrip('/'))
            if isinstance(entry, dict) and entry.get('xpath'):
                observed.add(_strip_leading_slash(str(entry['xpath'])).rstrip('/'))
    observed = set(
        filter_unsupported_qos_xpaths(observed, asic_model=asic_model)
    )

    def _xpath_under_prefix(xpath: str, prefix: str) -> bool:
        """True if *xpath* is the prefix node or a descendant (incl. list keys).

        gNMI often returns keyed list instances, e.g. subscribe
        ``.../headroom-buffer`` yields
        ``.../headroom-buffer[buffer-type=primary]/state/...``.
        """
        if xpath == prefix:
            return True
        if xpath.startswith(prefix + '/') or xpath.startswith(prefix + '['):
            return True
        return False

    missing = [
        prefix
        for prefix in expected
        if not any(_xpath_under_prefix(xpath, prefix) for xpath in observed)
    ]
    allure.attach(
        '\n'.join(sorted(observed)),
        'qos_prefix_subscription_observed_xpaths',
    )
    assert not missing, (
        'QoS prefix subscription returned no updates for %s; observed=%s'
        % (missing, sorted(observed))
    )


def validate_component_top_level_subscription(
    *,
    expected_xpaths: Sequence[str],
    dut=None,
    series_key: str = 'gnmi-series-component',
    skip_rate_metrics_first_timestamp: bool = False,
) -> None:
    """SSIM3 components ``collect_verify_bulk_subscription`` (thin wrapper of the common core).

    ``/components`` streams a stable, fully modeled xpath set (no process-PID churn), so the strict
    unexpected check runs with no tolerance (``unexpected_filter=None``). The only component-specific
    concern lives in the noise tuple: SSIM3 filters ``integrated-circuit`` (and the other
    platform-variant substrings) from the received stream — ``build_components_top_level_xpaths``
    already drops those leaves from the expected set to match, so they count toward neither check.

    One coverage tolerance is applied via ``missing_filter``: ``_component_known_device_gap_xpaths``
    (currently copper-transceiver ``ethernet-pmd``). Those leaves stay in the expected model (SSIM3
    parity) but are logged and tolerated when persistently absent, since the platform has no value to
    stream for them — see :data:`_TRANSCEIVER_ETHERNET_PMD_SUFFIX`.

    Known-gap xpaths are computed from the collection-time ``gnmi-component-context`` cache. On cache
    miss, ``dut`` (when supplied) is used for live ``discover_components_top_level_context`` so
    copper ``ethernet-pmd`` tolerance stays active — same fallback as
    ``build_smoke_component_expected_xpaths``.
    """
    context: Optional[Dict[str, Any]] = None
    try:
        cached = telemetryCache.get_data(_COMPONENT_CONTEXT_CACHE_KEY)
    except KeyError:
        cached = None
    if isinstance(cached, dict):
        context = cached
    if context is None and dut is not None:
        context = discover_components_top_level_context(dut)
    known_gaps = _component_known_device_gap_xpaths(context)

    def _component_missing_filter(persistent_missing: set, received_union: set) -> set:
        tolerated = persistent_missing & known_gaps
        if tolerated:
            logger.warning(
                'Tolerating %d persistently-missing component xpath(s) as KNOWN device gaps '
                '(kept in the SSIM3 expected model, not streamed by this platform): %s',
                len(tolerated), sorted(tolerated),
            )
        return persistent_missing - tolerated

    validate_top_level_subscription(
        expected_xpaths=expected_xpaths,
        series_key=series_key,
        model_label='component',
        noise_substrings=_COMPONENT_NOISE_XPATH_SUBSTRINGS,
        skip_rate_metrics_first_timestamp=skip_rate_metrics_first_timestamp,
        missing_filter=_component_missing_filter if known_gaps else None,
    )


def discover_bgp_neighbor_address(dut, *, vrf: str = 'default') -> Optional[str]:
    """Return first BGP neighbor address from NVUE/OpenAPI show (mlx lab)."""
    neighbor_node = BaseComponent(Vrf().vrf_id[vrf], path='/router/bgp/neighbor')
    payload = _show_json(neighbor_node, dut)
    if not isinstance(payload, dict) or not payload:
        return None
    for neighbor in sorted(payload.keys()):
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', neighbor) or ':' in neighbor:
            return neighbor
    return None


def discover_lldp_neighbor_context(dut, *, preferred_iface: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Discover first LLDP neighbor on a front-panel interface."""
    candidates: List[str] = []
    if preferred_iface:
        candidates.append(preferred_iface)
    candidates.extend(discover_swp_interfaces_on_dut(dut))
    seen = set()
    for iface in candidates:
        if iface in seen:
            continue
        seen.add(iface)
        payload = _show_json(Interface(None, port_name=iface).lldp.neighbor, dut)
        if not isinstance(payload, dict) or not payload:
            continue
        for neighbor_key in payload:
            neighbor_id, _, port = neighbor_key.partition(':')
            if not neighbor_id:
                neighbor_id = neighbor_key
                port = iface
            mgmt_address = None
            neighbor_obj = payload.get(neighbor_key) or {}
            chassis = neighbor_obj.get('chassis') or {}
            mgmt_ifinfo = chassis.get('management-ifinfo') or {}
            if isinstance(mgmt_ifinfo, dict):
                for _, entry in mgmt_ifinfo.items():
                    if isinstance(entry, dict):
                        mgmt_address = entry.get('management-address-ipv4') or entry.get('management-address-ipv6')
                        if mgmt_address:
                            break
            return {
                'interface': iface,
                'neighbor_id': neighbor_id,
                'port': port or iface,
                'mgmt_address': mgmt_address,
            }
    return None


def _first_sys_block_disk(dut, pattern: str) -> str:
    """First ``/sys/block`` name matching ``grep -E`` ``pattern``, else empty."""
    out = dut.run_cmd(
        "ls /sys/block 2>/dev/null | grep -E %s | head -1" % (shlex.quote(pattern),),
        validate=False,
        print_output=False,
    )
    line = (out or '').strip().splitlines()
    return line[0].strip() if line else ''


def _looks_like_block_device(name: str) -> bool:
    """True for kernel disk names that appear in ``/proc/diskstats`` (not NVUE inventory labels)."""
    low = name.lower()
    return 'nvme' in low or low.startswith('sd') or low.startswith('vd')


def discover_storage_component_name(dut) -> str:
    """Kernel block device used as the OpenConfig storage component (``nvme*`` / ``sd*``).

    Prefer ``/sys/block`` so the name matches ``/proc/diskstats``. NVUE
    ``platform component`` keys are used only when they already look like a block
    device — labels such as ``Storage`` are not kernel devices and would skip the
    CLI cross-check.
    """
    if dut_root_on_nvme_storage(dut):
        name = _first_sys_block_disk(dut, '^nvme')
        if name:
            return name
    for pattern in ('^sd[a-z]+$', '^nvme', '^vd[a-z]+$'):
        name = _first_sys_block_disk(dut, pattern)
        if name:
            return name
    payload = _show_json(BaseComponent(System(), path='/platform/component'), dut)
    if isinstance(payload, dict):
        for key in sorted(payload):
            low = key.lower()
            if 'nvme' in low or low.startswith('sd') or 'storage' in low:
                return key
    return 'sda'


def list_psu_component_names(dut) -> List[str]:
    """Present PSU inventory keys (``PSU1``, ``PSU2``, …) from NVUE platform data."""
    names: List[str] = []
    seen = set()
    for node in (
        Platform().inventory,
        BaseComponent(System(), path='/platform/component'),
    ):
        payload = _show_json(node, dut)
        if not isinstance(payload, dict):
            continue
        for key, entry in payload.items():
            name = str(key).strip()
            if not name.upper().startswith('PSU') or name in seen:
                continue
            if isinstance(entry, dict):
                state = str(entry.get('state') or '').strip().lower()
                if state in ('absent', 'bad', 'empty', 'not present'):
                    continue
            seen.add(name)
            names.append(name)
        if names:
            break
    return names


def discover_psu_component_name(dut) -> Optional[str]:
    """Pick one present PSU at random for SSIM test_10 (None when the DUT has none)."""
    global _SELECTED_PSU_COMPONENT_NAME
    names = list_psu_component_names(dut)
    if not names:
        _SELECTED_PSU_COMPONENT_NAME = None
        logger.warning('No present PSU inventory components found on DUT')
        return None
    chosen = RandomizationTool.select_random_value(names).get_returned_value()
    _SELECTED_PSU_COMPONENT_NAME = chosen
    logger.info('Selected PSU component for platform hw info: %s (from %s)', chosen, names)
    return chosen


def get_selected_psu_component_name() -> Optional[str]:
    """PSU name chosen by ``discover_psu_component_name`` / ``build_component_system_gnmi_jobs``."""
    return _SELECTED_PSU_COMPONENT_NAME


def ensure_gnmic_installed_on_collector(server) -> str:
    """Install ``gnmic`` on ``engines.sonic_mgmt`` when missing (grpc-tunnel dial-out)."""
    with allure.step('Ensure gnmic is installed on sonic-mgmt collector'):
        gnmic_bin = EngineAdapterTool.run_cmd(
            server,
            'command -v gnmic 2>/dev/null || true',
            timeout=30,
        ).strip()
        if gnmic_bin:
            logger.info('gnmic found on collector: %s', gnmic_bin)
            return gnmic_bin

        EngineAdapterTool.run_cmd(
            server,
            'bash -c "$(curl -sL https://get-gnmic.openconfig.net)" -- -v %s'
            % (_GNMIC_INSTALL_VERSION,),
            timeout=300,
        )
        gnmic_bin = EngineAdapterTool.run_cmd(
            server,
            'command -v gnmic 2>/dev/null || true',
            timeout=30,
        ).strip()
        if not gnmic_bin:
            pytest.fail(
                'gnmic not available on sonic-mgmt after install attempt; '
                'grpc-tunnel gNMI coexistence cannot run'
            )
        logger.info('gnmic installed on collector: %s', gnmic_bin)
        return gnmic_bin


def _collector_gnmic_base(collector: GrpcTunnelServer, gnmic_bin: str) -> str:
    """Like ``GrpcTunnelServer._gnmic_base`` but with an explicit remote gnmic path."""
    conf_dir = os.path.dirname(collector.config_path)
    conf_name = os.path.basename(collector.config_path)
    inner = '%s --config %s --use-tunnel-server' % (
        shlex.quote(gnmic_bin),
        shlex.quote(conf_name),
    )
    if conf_dir:
        return 'cd %s && %s' % (shlex.quote(conf_dir), inner)
    return inner


def _collector_gnmic_pkill_pattern(collector: GrpcTunnelServer) -> str:
    return 'gnmic --config %s' % (os.path.basename(collector.config_path),)


def _kill_stale_collector_gnmic(collector: GrpcTunnelServer) -> None:
    """Free the tunnel listen port; prior failed fixture setup may leave gnmic running."""
    assert collector.server is not None
    listen_port = GrpcTunnelServer.listen_port(collector.tunnel_address)
    pat = _collector_gnmic_pkill_pattern(collector)
    quoted = shlex.quote(pat)
    EngineAdapterTool.run_cmd(
        collector.server,
        (
            'pkill -INT -f %s 2>/dev/null || true; sleep 1; '
            'pkill -TERM -f %s 2>/dev/null || true; sleep 1; '
            'pkill -9 -f %s 2>/dev/null || true; '
            'fuser -k %d/tcp 2>/dev/null || true; sleep 1'
        ) % (quoted, quoted, quoted, listen_port),
        timeout=90,
    )


def _wait_for_collector_gnmic_listener(
    collector: GrpcTunnelServer,
    *,
    timeout_sec: int = 20,
) -> bool:
    """Return True when gnmic tunnel-server is listening on the configured port."""
    assert collector.server is not None
    listen_port = GrpcTunnelServer.listen_port(collector.tunnel_address)
    marker = ':%d' % (listen_port,)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        out = EngineAdapterTool.run_cmd(
            collector.server,
            (
                "ss -tln 2>/dev/null | grep -F '%s' || "
                "netstat -tln 2>/dev/null | grep -F '%s' || true"
            ) % (marker, marker),
            timeout=30,
        )
        if marker in (out or ''):
            return True
        time.sleep(1)
    return False


def _assert_collector_gnmic_listener(
    collector: GrpcTunnelServer,
    *,
    timeout_sec: int = 20,
    startup_log: str = '',
) -> None:
    listen_port = GrpcTunnelServer.listen_port(collector.tunnel_address)
    if _wait_for_collector_gnmic_listener(collector, timeout_sec=timeout_sec):
        return
    assert collector.server is not None
    owners = EngineAdapterTool.run_cmd(
        collector.server,
        (
            "ss -tlnp 2>/dev/null | grep ':%d' || "
            "fuser %d/tcp 2>/dev/null || true"
        ) % (listen_port, listen_port),
        timeout=30,
    )
    pytest.fail(
        'gnmic tunnel-server not listening on :%d after %ds; port owners: %s; gnmic output: %s'
        % (
            listen_port,
            timeout_sec,
            (owners or '').strip()[:500],
            (startup_log or '<empty>').strip()[-500:],
        )
    )


def _dut_grpc_tunnel_unit(tunnel_name: str) -> str:
    return 'nv-grpctunneld@%s' % (tunnel_name,)


def _dut_grpc_tunnel_docker_name(tunnel_name: str) -> str:
    return 'nv-grpctunnel-%s' % (tunnel_name,)


def _resolve_dut_gnmi_basic_credentials(dut) -> Tuple[str, str]:
    """SSIM ``GnmiCollectorDialOut*`` uses the DUT NVUE user for gnmic Basic auth."""
    username = getattr(dut, 'username', None) or getattr(dut, 'default_username', None)
    password = getattr(dut, 'password', None) or getattr(dut, 'default_password', None)
    if not username or not password:
        try:
            from ngts.tools.TestToolkit import TestToolkit

            device = TestToolkit.devices.dut
            username = username or device.default_username
            password = password or device.default_password
        except Exception:
            pass
    if not username or not password:
        pytest.fail('Could not resolve DUT username/password for gnmic Basic auth (SSIM cumulus parity)')
    return str(username), str(password)


def _dut_grpc_tunnel_cfg_path(tunnel_name: str) -> str:
    return '/etc/nv-grpctunnel/config/%s.cfg' % (tunnel_name,)


def _dut_shell_lines(text: str) -> list:
    """Non-empty stripped lines from DUT shell output (ignores bash cleanup noise)."""
    return [ln.strip() for ln in (text or '').splitlines() if ln.strip()]


def _systemctl_is_active(dut, unit: str) -> str:
    """Return systemctl state; cumulus shells may append unrelated bash errors after the state line."""
    out = dut.run_cmd('systemctl is-active %s' % (unit,), validate=False) or ''
    for line in _dut_shell_lines(out):
        if line in ('active', 'activating', 'inactive', 'failed', 'dead', 'unknown'):
            return line
    return ''


def _assert_dut_grpc_tunnel_cfg_present(dut, tunnel_name: str) -> None:
    """``nv-grpctunneld@`` requires ``ConditionFileNotEmpty`` on the per-server cfg."""
    cfg = _dut_grpc_tunnel_cfg_path(tunnel_name)
    check = dut.run_cmd(
        'test -s %s && echo OK || echo MISSING' % (shlex.quote(cfg),),
        validate=False,
    ) or ''
    if 'OK' in check:
        return
    listing = dut.run_cmd(
        'ls -la /etc/nv-grpctunnel/config/ 2>/dev/null || true',
        validate=False,
    )
    pytest.fail(
        'gRPC tunnel client config %s missing after NVUE apply; '
        'nv-grpctunneld@%s was skipped (ConditionFileNotEmpty). '
        'config dir listing: %s'
        % (cfg, tunnel_name, (listing or '').strip()[:800])
    )


def _configure_dut_grpc_tunnel(
    dut,
    sonic_mgmt_ip: str,
    tunnel_name: str,
    listen_port: int,
    *,
    use_mtls: bool = True,
    is_nvos: bool = False,
) -> None:
    """NVUE grpc-tunnel server; mTLS uses SSIM ``configure_grpc_tunnel_vx`` field parity."""
    if use_mtls:
        from ngts.tests_nvos.system.telemetry.otel.cumulus.gnmi_dialout_mtls import (
            configure_dut_grpc_tunnel_mtls,
        )

        configure_dut_grpc_tunnel_mtls(dut, sonic_mgmt_ip, tunnel_name, listen_port)
    else:
        from ngts.nvos_tools.system.System import System
        from ngts.tests_nvos.system.grpc_tunnel.constants import GrpcTunnelConstants

        with allure.step('Configure grpc-tunnel server %s (NVUE API)' % (tunnel_name,)):
            system = System()
            system.grpc_tunnel.server.set_new_tunnel(tunnel_name=tunnel_name)
            system.grpc_tunnel.server.tunnel_name[tunnel_name].set(
                op_param_name=GrpcTunnelConstants.ADDRESS,
                op_param_value=sonic_mgmt_ip,
            )
            system.grpc_tunnel.server.tunnel_name[tunnel_name].set(
                op_param_name=GrpcTunnelConstants.PORT,
                op_param_value=str(listen_port),
                apply=True,
            )
    if is_nvos:
        # NVOS runs the dial-out client as a ``nv-grpctunnel-<name>`` docker container started
        # by ``nv config apply`` -- there is no ``nv-grpctunneld@`` systemd unit or per-server
        # ``.cfg`` file (that is the Cumulus/SSIM model), so wait on the container instead.
        _wait_dut_grpc_tunnel_container_running(dut, tunnel_name)
        return
    _assert_dut_grpc_tunnel_cfg_present(dut, tunnel_name)
    _pause_dut_grpc_tunnel_client(dut, tunnel_name)


def _pause_dut_grpc_tunnel_client(dut, tunnel_name: str) -> None:
    """Stop the DUT dial-out client until gnmic tunnel-server is listening (SSIM ordering)."""
    unit = _dut_grpc_tunnel_unit(tunnel_name)
    dut.run_cmd('sudo systemctl stop %s' % (unit,), validate=False)


def _wait_dut_grpc_tunnel_container_running(
    dut,
    tunnel_name: str,
    *,
    timeout_sec: int = _GRPC_TUNNEL_CONTAINER_WAIT_SEC,
) -> None:
    """NVOS: ``nv config apply`` starts the ``nv-grpctunnel-<name>`` container asynchronously."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _is_dut_grpc_tunnel_client_running(dut, tunnel_name):
            return
        time.sleep(_GRPC_TUNNEL_CONTAINER_POLL_SEC)
    docker_name = _dut_grpc_tunnel_docker_name(tunnel_name)
    listing = dut.run_cmd(
        "docker ps -a --format '{{.Names}}\t{{.Status}}' 2>/dev/null || true",
        validate=False,
    )
    pytest.fail(
        'gRPC tunnel client container %s not running %ds after NVUE apply. '
        'docker ps -a: %s'
        % (docker_name, timeout_sec, (listing or '').strip()[:_DUT_OUTPUT_EXCERPT_CHARS])
    )


def _unset_dut_grpc_tunnel(dut, tunnel_name: str, *, is_nvos: bool = False) -> None:
    if not is_nvos:
        unit = _dut_grpc_tunnel_unit(tunnel_name)
        try:
            dut.run_cmd('sudo systemctl stop %s' % (unit,), validate=False)
        except Exception:
            pass
    try:
        from ngts.nvos_tools.system.System import System

        System().grpc_tunnel.server.tunnel_name[tunnel_name].unset(
            apply=True, ask_for_confirmation=True, dut_engine=dut
        )
    except Exception as exc:
        logger.warning('grpc-tunnel unset %s: %s', tunnel_name, exc)


def _is_dut_grpc_tunnel_client_running(dut, tunnel_name: str) -> bool:
    unit = _dut_grpc_tunnel_unit(tunnel_name)
    state = _systemctl_is_active(dut, unit)
    if state in ('active', 'activating'):
        return True
    docker_name = _dut_grpc_tunnel_docker_name(tunnel_name)
    for ps_cmd in (
        "sudo docker ps --format '{{.Names}}'",
        "docker ps --format '{{.Names}}'",
    ):
        out = dut.run_cmd(
            '%s 2>/dev/null | grep -Fx %s || true' % (ps_cmd, docker_name),
            validate=False,
        ) or ''
        if docker_name in _dut_shell_lines(out):
            return True
    return False


def _start_dut_grpc_tunnel(dut, tunnel_name: str) -> None:
    """Start tunnel client; ``nv config apply`` alone may leave the unit inactive (dead)."""
    unit = _dut_grpc_tunnel_unit(tunnel_name)
    dut.run_cmd('sudo systemctl start %s' % (unit,), validate=False)
    deadline = time.time() + 20
    while time.time() < deadline:
        if _is_dut_grpc_tunnel_client_running(dut, tunnel_name):
            return
        time.sleep(2)
    journal = dut.run_cmd(
        'sudo journalctl -u %s -n 20 --no-pager 2>/dev/null || true' % (unit,),
        validate=False,
    )
    cfg = _dut_grpc_tunnel_cfg_path(tunnel_name)
    pytest.fail(
        'gRPC tunnel client %s not running after systemctl start (cfg=%s). journal: %s'
        % (tunnel_name, cfg, (journal or '').strip()[-800:])
    )


def _restart_dut_grpc_tunnel(dut, tunnel_name: str, *, is_nvos: bool = False) -> None:
    """Force an immediate dial-out attempt after gnmic is listening (SSIM ``restart_grpctunnel``)."""
    if is_nvos:
        # NVOS: bounce the dial-out container to retry immediately (no systemd template unit).
        docker_name = _dut_grpc_tunnel_docker_name(tunnel_name)
        for restart_cmd in (
            'sudo docker restart %s' % (docker_name,),
            'docker restart %s' % (docker_name,),
        ):
            out = dut.run_cmd('%s 2>/dev/null || true' % (restart_cmd,), validate=False) or ''
            if docker_name in _dut_shell_lines(out):
                break
        time.sleep(_GRPC_TUNNEL_CONTAINER_POLL_SEC)
        _wait_dut_grpc_tunnel_container_running(dut, tunnel_name)
        return
    unit = _dut_grpc_tunnel_unit(tunnel_name)
    dut.run_cmd('sudo systemctl stop %s' % (unit,), validate=False)
    time.sleep(2)
    _start_dut_grpc_tunnel(dut, tunnel_name)


def _grpc_tunnel_connection_status(dut, tunnel_name: str) -> Dict[str, str]:
    """Read ``register``/``tunnel`` from ``System().grpc_tunnel.server``."""
    data = _show_json(System().grpc_tunnel.server.tunnel_name[tunnel_name], dut)
    if not isinstance(data, dict):
        return {}
    conn = (data.get('status') or {}).get('connection') or {}
    return {
        'register': str(conn.get('register') or ''),
        'tunnel': str(conn.get('tunnel') or ''),
    }


def _wait_for_grpc_tunnel_connection(
    dut,
    tunnel_name: str,
    *,
    timeout_sec: int = 45,
) -> bool:
    """Return True when grpc-tunnel show reports register/tunnel yes for the server."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        conn = _grpc_tunnel_connection_status(dut, tunnel_name)
        if conn.get('register') == 'yes' and conn.get('tunnel') == 'yes':
            return True
        time.sleep(3)
    return False


def _assert_grpc_tunnel_connected(
    dut,
    tunnel_name: str,
    *,
    listen_port: int,
    timeout_sec: int = 90,
    sonic_mgmt=None,
    gnmic_log_paths: Optional[Sequence[str]] = None,
) -> None:
    """Hard-fail when DUT grpc-tunnel client never registers with gnmic."""
    if _wait_for_grpc_tunnel_connection(dut, tunnel_name, timeout_sec=timeout_sec):
        logger.info('gRPC tunnel %s connected to gnmic on :%d', tunnel_name, listen_port)
        return
    conn = _grpc_tunnel_connection_status(dut, tunnel_name)
    unit = _dut_grpc_tunnel_unit(tunnel_name)
    docker_name = _dut_grpc_tunnel_docker_name(tunnel_name)
    journal = dut.run_cmd(
        'sudo journalctl -u %s -n 30 --no-pager 2>/dev/null || true' % (unit,),
        validate=False,
    )
    docker_logs = dut.run_cmd(
        'sudo docker logs --tail 40 %s 2>&1 || true' % (docker_name,),
        validate=False,
    )
    gnmi_payload = _show_json(System().gnmi_server, dut)
    if isinstance(gnmi_payload, dict):
        gnmi_server = json.dumps(gnmi_payload)
    else:
        gnmi_server = str(gnmi_payload or '')
    gnmic_tails = []
    if sonic_mgmt is not None and gnmic_log_paths:
        for log_path in gnmic_log_paths:
            tail = EngineAdapterTool.run_cmd(
                sonic_mgmt,
                'tail -c 2500 %s 2>/dev/null || true' % (shlex.quote(log_path),),
                timeout=60,
            )
            if (tail or '').strip():
                gnmic_tails.append('%s: %s' % (log_path, (tail or '').strip()[-2000:]))
    pytest.fail(
        'gRPC tunnel %s did not reach register/tunnel=yes within %ds (collector :%d). '
        'last status=%s gnmi-server: %s journal tail: %s docker logs: %s gnmic logs: %s'
        % (
            tunnel_name,
            timeout_sec,
            listen_port,
            conn,
            (gnmi_server or '').strip()[-600:],
            (journal or '').strip()[-1200:],
            (docker_logs or '').strip()[-800:],
            ' | '.join(gnmic_tails)[:2500] if gnmic_tails else '(no gnmic sample log yet)',
        )
    )


def _setup_gnmi_dialout_mtls(dut, sonic_mgmt):
    """One-time mTLS cert exchange + gnmi-server enable (SSIM pre_suite dial-out stack)."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.gnmi_dialout_mtls import (
        configure_dut_gnmi_server_mtls,
        generate_gnmi_dialout_mtls_material,
        install_gnmi_dialout_certs_on_dut,
        resolve_dut_ip,
        upload_collector_mtls_certs,
    )

    dut_ip = resolve_dut_ip(dut)
    material = generate_gnmi_dialout_mtls_material(
        collector_ip=sonic_mgmt.ip,
        dut_ip=dut_ip,
    )
    upload_collector_mtls_certs(sonic_mgmt, material)
    install_gnmi_dialout_certs_on_dut(dut, material, dut_ip=dut_ip)
    configure_dut_gnmi_server_mtls(dut, dut_ip=dut_ip)
    return material, dut_ip


def setup_gnmi_coexistence_session(
    dut,
    sonic_mgmt,
    jobs: Sequence[GnmiCollectionJob],
    *,
    use_mtls: bool = True,
    is_nvos: bool = False,
) -> GnmiCoexistenceSession:
    """Create NVUE grpc-tunnel servers and gnmic collectors for each job."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.gnmi_dialout_mtls import (
        GNMI_DIALOUT_TARGET_WAIT,
    )

    session = GnmiCoexistenceSession(is_nvos=is_nvos)
    session.gnmic_bin = ensure_gnmic_installed_on_collector(sonic_mgmt)
    gnmi_user, gnmi_pass = _resolve_dut_gnmi_basic_credentials(dut)
    mtls_material = None
    if use_mtls and jobs:
        mtls_material, _dut_ip = _setup_gnmi_dialout_mtls(dut, sonic_mgmt)
        session.mtls_configured = True

    configured_tunnels = set()
    with allure.step('Configure grpc-tunnel + gnmic dial-out collectors'):
        for job in jobs:
            if job.tunnel_name not in configured_tunnels:
                _configure_dut_grpc_tunnel(
                    dut,
                    sonic_mgmt.ip,
                    job.tunnel_name,
                    job.listen_port,
                    use_mtls=use_mtls,
                    is_nvos=is_nvos,
                )
                configured_tunnels.add(job.tunnel_name)
            if use_mtls and mtls_material is not None:
                # SSIM GnmiCollectorDialOut* always sets skip_verify=True: tunneled target is
                # ``:9339`` (localhost) but gnmi-server cert SAN is the DUT management IP.
                _kill_stale_collector_gnmic(
                    GrpcTunnelServer(
                        username=gnmi_user,
                        password=gnmi_pass,
                        tunnel_address=':%d' % (job.listen_port,),
                        tunnel_name=job.tunnel_name,
                        server=sonic_mgmt,
                    )
                )
                collector = GrpcTunnelServer(
                    username=gnmi_user,
                    password=gnmi_pass,
                    tunnel_address=':%d' % (job.listen_port,),
                    tunnel_name=job.tunnel_name,
                    server=sonic_mgmt,
                    skip_verify=True,
                    target_wait_time=GNMI_DIALOUT_TARGET_WAIT,
                    tls_cert_file=mtls_material.remote_gnmic_target_crt,
                    tls_key_file=mtls_material.remote_gnmic_target_key,
                    tls_ca_file=mtls_material.remote_dut_ca_crt,
                    gnmi_tls_ca_file=mtls_material.remote_dut_ca_crt,
                    gnmi_tls_cert_file=mtls_material.remote_gnmic_client_crt,
                    gnmi_tls_key_file=mtls_material.remote_gnmic_client_key,
                    grpc_auth=True,
                )
                collector.write_config()
            else:
                collector = GrpcTunnelServer(
                    username=gnmi_user,
                    password=gnmi_pass,
                    tunnel_address=':%d' % (job.listen_port,),
                    tunnel_name=job.tunnel_name,
                    server=sonic_mgmt,
                )
                collector.prepare_with_tls()
            session.setups.append(
                GrpcTunnelServerSetup(
                    tunnel_name=job.tunnel_name,
                    listen_port=job.listen_port,
                    collector=collector,
                )
            )
            time.sleep(2)
    return session


def teardown_gnmi_coexistence_session(dut, session: GnmiCoexistenceSession) -> None:
    with allure.step('Teardown gNMI coexistence grpc-tunnels'):
        for setup in session.setups:
            try:
                setup.collector.stop_subscription()
            except Exception:
                pass
            try:
                setup.collector.delete()
            except Exception:
                pass
            _unset_dut_grpc_tunnel(dut, setup.tunnel_name, is_nvos=session.is_nvos)
        session.setups.clear()
        if session.mtls_configured:
            from ngts.tests_nvos.system.telemetry.otel.cumulus.gnmi_dialout_mtls import (
                _release_stale_gnmi_dialout_security,
            )

            _release_stale_gnmi_dialout_security(dut)
            session.mtls_configured = False


def _remote_gnmi_log_path(tunnel_name: str, model: str) -> str:
    return '/tmp/%s_gnmi_%s_sample.json' % (tunnel_name, model)


def _start_gnmic_sample_subscription(
    collector: GrpcTunnelServer,
    *,
    model: str,
    paths: Sequence[str],
    sample_interval: str,
    duration_sec: int,
    gnmic_bin: str = 'gnmic',
    encoding: str = 'json',
) -> str:
    out_path = _remote_gnmi_log_path(collector.tunnel_name, model)
    _kill_stale_collector_gnmic(collector)
    paths_cli = ' '.join('--path %s' % (shlex.quote(p),) for p in paths)
    mtls_suffix = collector.gnmi_mtls_cli_suffix()
    inner = (
        '%s%s -d subscribe --mode stream --stream-mode sample -i %s --encoding %s %s '
        '> %s 2>&1'
    ) % (
        _collector_gnmic_base(collector, gnmic_bin),
        mtls_suffix,
        shlex.quote(sample_interval),
        shlex.quote(encoding),
        paths_cli,
        shlex.quote(out_path),
    )
    start_cmd = (
        'OUT=%s; rm -f "$OUT"; nohup timeout --foreground %d bash -c %s >/dev/null 2>&1 &'
    ) % (shlex.quote(out_path), int(duration_sec), shlex.quote(inner))
    assert collector.server is not None
    startup_log = ''
    for attempt in (1, 2):
        EngineAdapterTool.run_cmd(collector.server, start_cmd, timeout=60)
        if _wait_for_collector_gnmic_listener(collector, timeout_sec=25):
            return out_path
        startup_log = _read_remote_file(collector.server, out_path)
        logger.warning(
            'gnmic tunnel-server for %s/%s did not bind on attempt %d; gnmic output: %s',
            collector.tunnel_name, model, attempt, (startup_log or '<empty>').strip()[-500:],
        )
        _kill_stale_collector_gnmic(collector)
    _assert_collector_gnmic_listener(collector, timeout_sec=25, startup_log=startup_log)
    return out_path


def _read_remote_file(server, path: str) -> str:
    return EngineAdapterTool.run_cmd(
        server,
        'cat %s 2>/dev/null || true' % (shlex.quote(path),),
        timeout=120,
    )


def _wait_for_remote_file(server, path: str, *, min_bytes: int = 64, timeout_sec: int = 30) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        out = EngineAdapterTool.run_cmd(
            server,
            'test -s %s && stat -c %%s %s || echo 0' % (shlex.quote(path), shlex.quote(path)),
            timeout=30,
        )
        try:
            if int((out or '0').strip().splitlines()[-1]) >= min_bytes:
                return
        except ValueError:
            pass
        time.sleep(2)


def _gnmi_log_has_unsupported_yang_paths(log_content: str) -> bool:
    """True when gnmic connected but the DUT YANG tree lacks the subscribed paths."""
    return 'does not exist in yang models' in (log_content or '').lower()


def _populate_gnmi_cache_from_session(
    sonic_mgmt,
    jobs: Sequence[GnmiCollectionJob],
    session: GnmiCoexistenceSession,
    out_paths: Dict[str, str],
) -> None:
    timing: Dict[str, Any] = {}
    for job, _setup in zip(jobs, session.setups):
        log_path = out_paths[job.model]
        _wait_for_remote_file(sonic_mgmt, log_path, timeout_sec=45)
        log_content = _read_remote_file(sonic_mgmt, log_path)
        series = parse_gnmic_log_to_series(log_content)
        if not series and log_content.strip():
            lowered = log_content.lower()
            if 'command not found' in lowered or 'no such file' in lowered:
                pytest.fail(
                    'gnmic failed on sonic-mgmt for %s (%s): %s'
                    % (job.model, log_path, log_content.strip()[:500])
                )
            if 'authentication handshake failed' in lowered or 'certificate is not valid' in lowered:
                pytest.fail(
                    'gnmic TLS handshake failed for %s (%s); tunnel mTLS is OK but tunneled '
                    'gnmi-server cert must use skip-verify (SSIM dial-out parity). tail: %s'
                    % (job.model, log_path, log_content.strip()[-800:])
                )
            if 'bind: address already in use' in lowered:
                pytest.fail(
                    'gnmic tunnel-server port conflict for %s (%s); stale gnmic still bound. tail: %s'
                    % (job.model, log_path, log_content.strip()[-800:])
                )
            if 'certificate required' in lowered or 'tls: certificate required' in lowered:
                pytest.fail(
                    'gnmic tunneled gnmi-server mTLS failed for %s (%s); need --tls-cert/--tls-key '
                    '(gnmic_client signed by gnmic_ca, SSIM GnmiCollectorDialOutVX parity). tail: %s'
                    % (job.model, log_path, log_content.strip()[-800:])
                )
            if 'unauthenticated' in lowered or 'authentication failed' in lowered:
                pytest.fail(
                    'gnmic gnmi-server auth failed for %s (%s); SSIM dial-out uses grpc_auth=True '
                    '(mTLS + YAML username/password, no --auth-scheme Basic). tail: %s'
                    % (job.model, log_path, log_content.strip()[-800:])
                )
            if _gnmi_log_has_unsupported_yang_paths(log_content):
                logger.warning(
                    'gNMI %s: subscribed paths are absent from DUT YANG (%s); '
                    'skipping cache (mlx-lab / build parity)',
                    job.model,
                    log_path,
                )
                continue
        if not series:
            if _gnmi_log_has_unsupported_yang_paths(log_content):
                logger.warning(
                    'gNMI %s: no samples and YANG paths unsupported on DUT (%s)',
                    job.model,
                    log_path,
                )
                continue
            if job.optional:
                logger.warning(
                    'gNMI %s (optional): no samples on this DUT (%s); tail=%s',
                    job.model,
                    log_path,
                    (log_content or '').strip()[-500:],
                )
                continue
            pytest.fail(
                'gNMI %s produced no timestamps (%s); check grpc-tunnel register/tunnel and gnmic logs'
                % (job.model, log_path)
            )
        telemetryCache.add_data('gnmi-series-%s' % (job.model,), series)
        timing[job.model] = _timing_summary_from_series(
            job.model,
            series,
            sample_interval=job.sample_interval,
            tolerance=0.1,
            timeout_sec=job.duration_sec,
        )
        logger.info('gNMI %s: %d timestamps from %s', job.model, len(series), log_path)
    try:
        existing = telemetryCache.get_data('gnmi_timing')
    except KeyError:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing.update(timing)
    telemetryCache.add_data('gnmi_timing', existing)


def _capture_system_pids_during_window(dut, jobs: Sequence[GnmiCollectionJob]) -> None:
    """SSIM3 ``capture_pids_during_subscription`` — snapshot active-service PIDs *during* streaming.

    Runs only when a ``/system`` job is present. Taking the ``get_system_process_pid`` snapshot once
    the subscriptions are live (not at discovery time) is what lets ``filter_old_process_pids`` tell
    a genuinely-missing leaf (PID alive during the window) from a stale one (PID already gone),
    independently of the received stream. Best-effort: failures leave the cache empty and the
    validator falls back to the received stream.
    """
    if not any(job.model == 'system' for job in jobs):
        return
    try:
        captured = get_system_process_pid_map(dut)
        telemetryCache.add_data(_SYSTEM_CAPTURED_PIDS_CACHE_KEY, captured)
        logger.info(
            'Captured PIDs during subscription for %d active service(s): %s',
            len(captured), {svc: pids for svc, pids in captured.items()},
        )
    except Exception as exc:  # noqa: BLE001 - best-effort parity with SSIM3 (logs + continues)
        logger.warning('capture_pids_during_subscription failed: %s', exc)
        telemetryCache.add_data(_SYSTEM_CAPTURED_PIDS_CACHE_KEY, {})


def run_gnmi_with_parallel_work(
    dut,
    sonic_mgmt,
    jobs: Sequence[GnmiCollectionJob],
    work,
    *,
    startup_grace_sec: int = 20,
    defer_teardown: bool = True,
    use_mtls: bool = True,
    is_nvos: bool = False,
) -> Optional[GnmiCoexistenceSession]:
    """Start gnmic subscriptions, run ``work()`` (e.g. OTLP collect), then parse gNMI cache.

    When ``defer_teardown`` is True the gRPC tunnel + gnmic session stays up until the caller
    invokes ``teardown_gnmi_coexistence_session`` (SSIM ``post_suite_hook`` parity).

    ``use_mtls=False`` selects plain addr+port dial-out (NVOS coexistence / grpc_tunnel basic).
    """
    if not jobs:
        work()
        return None

    session = setup_gnmi_coexistence_session(
        dut, sonic_mgmt, jobs, use_mtls=use_mtls, is_nvos=is_nvos
    )
    out_paths: Dict[str, str] = {}
    succeeded = False
    try:
        max_job_sec = max(j.duration_sec for j in jobs)
        gnmi_run_sec = max_job_sec + startup_grace_sec
        with allure.step('Start gnmic sample subscriptions'):
            for job, setup in zip(jobs, session.setups):
                out_paths[job.model] = _start_gnmic_sample_subscription(
                    setup.collector,
                    model=job.model,
                    paths=job.paths,
                    sample_interval=job.sample_interval,
                    duration_sec=gnmi_run_sec,
                    gnmic_bin=session.gnmic_bin,
                    encoding=job.encoding,
                )
        from ngts.tests_nvos.system.telemetry.otel.cumulus.gnmi_dialout_mtls import (
            GNMI_DIALOUT_RETRY_INTERVAL,
        )

        # DUT docker dial-out can take retry_interval + container boot before register/tunnel yes.
        tunnel_connect_timeout = max(120, GNMI_DIALOUT_RETRY_INTERVAL * 3 + startup_grace_sec)
        restarted_tunnels = set()
        with allure.step('Restart DUT grpc-tunnel clients after gnmic is listening'):
            for job in jobs:
                if job.tunnel_name in restarted_tunnels:
                    continue
                restarted_tunnels.add(job.tunnel_name)
                _restart_dut_grpc_tunnel(dut, job.tunnel_name, is_nvos=is_nvos)
                time.sleep(2)
            for job in jobs:
                _assert_grpc_tunnel_connected(
                    dut,
                    job.tunnel_name,
                    listen_port=job.listen_port,
                    timeout_sec=tunnel_connect_timeout,
                    sonic_mgmt=sonic_mgmt,
                    gnmic_log_paths=[out_paths.get(job.model)] if job.model in out_paths else None,
                )
        _capture_system_pids_during_window(dut, jobs)
        work()
        time.sleep(min(startup_grace_sec, 10))
        with allure.step('Parse gnmic logs into telemetry cache'):
            _populate_gnmi_cache_from_session(sonic_mgmt, jobs, session, out_paths)
        succeeded = True
    finally:
        if not (defer_teardown and succeeded):
            teardown_gnmi_coexistence_session(dut, session)
    if defer_teardown and succeeded:
        return session
    return None


def run_gnmi_coexistence_collection(
    dut,
    sonic_mgmt,
    jobs: Sequence[GnmiCollectionJob],
    *,
    startup_grace_sec: int = 20,
    parallel_wait_sec: Optional[int] = None,
    defer_teardown: bool = False,
) -> Optional[GnmiCoexistenceSession]:
    """Run gnmic sample subscriptions; optionally overlap with an external OTLP window."""
    if not jobs:
        return None

    session = setup_gnmi_coexistence_session(dut, sonic_mgmt, jobs)
    out_paths: Dict[str, str] = {}
    succeeded = False
    try:
        with allure.step('Start gnmic sample subscriptions'):
            max_job_sec = max(j.duration_sec for j in jobs)
            gnmi_run_sec = max_job_sec + startup_grace_sec
            for job, setup in zip(jobs, session.setups):
                out_paths[job.model] = _start_gnmic_sample_subscription(
                    setup.collector,
                    model=job.model,
                    paths=job.paths,
                    sample_interval=job.sample_interval,
                    duration_sec=gnmi_run_sec,
                    gnmic_bin=session.gnmic_bin,
                    encoding=job.encoding,
                )
            if parallel_wait_sec is None:
                time.sleep(gnmi_run_sec)
            else:
                remaining = max(0, gnmi_run_sec - int(parallel_wait_sec))
                if remaining:
                    time.sleep(remaining)

        with allure.step('Parse gnmic logs into telemetry cache'):
            _populate_gnmi_cache_from_session(sonic_mgmt, jobs, session, out_paths)
        succeeded = True
    finally:
        if not (defer_teardown and succeeded):
            teardown_gnmi_coexistence_session(dut, session)
    if defer_teardown and succeeded:
        return session
    return None


def _validate_sample_stream_timing_intervals(
    intervals: Sequence[float],
    *,
    sample_interval: float,
    tolerance: float,
    timeout_sec: int = 0,
    update_count_tolerance: int = 4,
    count_validation: bool = True,
) -> None:
    """SSIM ``validate_stream_subscription_timing`` sample-mode rules (second-precision series)."""
    if sample_interval <= 0:
        raise AssertionError('Invalid sample_interval: %s' % (sample_interval,))
    if len(intervals) < 1:
        raise AssertionError('Insufficient subscription updates for stream timing validation')

    excessive_intervals = 0
    for i, interval in enumerate(intervals):
        if i > 0 and interval > (2 * sample_interval):
            raise AssertionError(
                'Interval %.2fs is more than twice the sample interval %ss'
                % (interval, sample_interval)
            )
        if interval > sample_interval:
            if abs(interval - (sample_interval + 1)) <= tolerance:
                continue
            if abs(interval - sample_interval) > tolerance:
                excessive_intervals += 1
                if excessive_intervals > 3:
                    raise AssertionError(
                        'Too many intervals (%d) exceed configured sample interval %ss '
                        'by more than tolerance ±%.2fs'
                        % (excessive_intervals, sample_interval, tolerance)
                    )

    if count_validation and timeout_sec > 0:
        expected_updates = int(timeout_sec // sample_interval)
        actual_updates = len(intervals) + 1
        if abs(actual_updates - expected_updates) > update_count_tolerance:
            raise AssertionError(
                'Number of sample updates (%d) differs from expected count (%d) '
                'by more than allowed tolerance (%d)'
                % (actual_updates, expected_updates, update_count_tolerance)
            )


def _timing_summary_from_series(
    model: str,
    series: Dict[str, Dict[str, Dict[str, Any]]],
    *,
    sample_interval: str,
    tolerance: float,
    timeout_sec: int = 0,
) -> Dict[str, Any]:
    """Build ``gnmi_timing[model]`` using SSIM ``compute_gnmi_time_gap_analysis`` semantics."""
    s_sec = _parse_interval_to_seconds(sample_interval) or 0.0
    result: Dict[str, Any] = {
        'model': model,
        'expected': {
            'sample_interval': s_sec,
            'tolerance': tolerance,
            'timeout': timeout_sec,
            'count_validation': False,
        },
        'result': 'fail',
    }
    if not series:
        result['error'] = 'empty series'
        return result
    ts_keys = sorted(set(int(k) for k in series if str(k).isdigit()))
    if len(ts_keys) < 2:
        result['error'] = 'too few timestamps (%d)' % (len(ts_keys),)
        return result

    intervals = [float(ts_keys[i + 1] - ts_keys[i]) for i in range(len(ts_keys) - 1)]
    try:
        _validate_sample_stream_timing_intervals(
            intervals,
            sample_interval=s_sec,
            tolerance=tolerance,
            timeout_sec=timeout_sec,
            update_count_tolerance=4,
            count_validation=False,
        )
        result['result'] = 'pass'
    except AssertionError as exc:
        result['error'] = str(exc)
    return result


def assert_gnmi_timing_pass(
    model: str,
    *,
    cache_key: str = 'gnmi_timing',
    context: str = '',
) -> None:
    """SSIM ``test_03_gnmi_interface_sample_interval_adherence`` parity."""
    try:
        timing = telemetryCache.get_data(cache_key)
    except KeyError:
        timing = None
    model_result = timing.get(model) if isinstance(timing, dict) else None
    label = '%s%s' % (model, (' (%s)' % context if context else ''))
    assert model_result is not None, 'Missing GNMI timing analysis for %s in telemetry cache' % (label,)
    assert model_result.get('result') == 'pass', (
        '%s sampling interval adherence failed: %s' % (model, model_result)
    )


def build_smoke_gnmi_jobs(dut, *, duration_sec: int = 120) -> List[GnmiCollectionJob]:
    """BGP + LLDP jobs for ``Test_Telemetry_Coexistence_Smoke`` (skip missing topo)."""
    jobs: List[GnmiCollectionJob] = []
    lab = resolve_cumulus_lab_interfaces_on_dut(dut)
    lldp_ctx = discover_lldp_neighbor_context(dut, preferred_iface=lab.test_iface)
    if lldp_ctx:
        lldp_kwargs = dict(lldp_ctx)
        lldp_paths = resolve_model_xpaths('lldp', SMOKE_LLDP_ATTRS, lldp_kwargs)
        if lldp_paths:
            jobs.append(
                GnmiCollectionJob(
                    model='lldp',
                    tunnel_name='coexistence_lldp',
                    listen_port=SMOKE_GNMI_PORT_LLDP,
                    paths=lldp_paths,
                    sample_interval='5s',
                    duration_sec=duration_sec,
                    attrs=SMOKE_LLDP_ATTRS,
                    populate_kwargs=lldp_kwargs,
                    group_size=1,
                )
            )

    bgp_neighbor = discover_bgp_neighbor_address(dut)
    if bgp_neighbor:
        bgp_kwargs = {
            'neighbor_address': bgp_neighbor,
            'vrf': 'default',
            'afi_safi_name': 'IPV4_UNICAST',
        }
        bgp_paths = resolve_model_xpaths('bgp', SMOKE_BGP_ATTRS, bgp_kwargs)
        if bgp_paths:
            jobs.append(
                GnmiCollectionJob(
                    model='bgp',
                    tunnel_name='coexistence_bgp',
                    listen_port=SMOKE_GNMI_PORT_BGP,
                    paths=bgp_paths,
                    sample_interval='3s',
                    duration_sec=duration_sec,
                    attrs=SMOKE_BGP_ATTRS,
                    populate_kwargs=bgp_kwargs,
                    group_size=1,
                )
            )

    # SSIM3 ``test_07_interface_top_level_subscription``: a single top-level subscription
    # to ``/interfaces/interface[name=<iface>]``. Expected-leaf coverage (full SSIM3
    # interface_xpath set) is validated strictly per timestamp from ``gnmi-series-interface``.
    iface = getattr(lab, 'test_iface', None)
    if iface:
        jobs.append(
            GnmiCollectionJob(
                model='interface',
                tunnel_name='coexistence_smoke_interface_top',
                listen_port=SMOKE_GNMI_PORT_INTERFACE_TOP,
                paths=[interface_top_level_xpath(iface)],
                sample_interval='15s',
                duration_sec=duration_sec,
                attrs=(),
                populate_kwargs={'interface': iface},
                group_size=1,
            )
        )

    # SSIM3 ``test_08_system_top_level_subscription``: a single top-level subscription to
    # ``/system``. The dynamic key set (CPU cores / mount points / active-service PIDs / nvue
    # version) is discovered here and cached so the expected-leaf enumeration in
    # ``build_smoke_system_expected_xpaths`` stays consistent with the subscription window.
    # Full SSIM3 system_xpath coverage is validated strictly per timestamp from
    # ``gnmi-series-system``.
    system_context = discover_system_top_level_context(dut)
    telemetryCache.add_data(_SYSTEM_CONTEXT_CACHE_KEY, system_context)
    jobs.append(
        GnmiCollectionJob(
            model='system',
            tunnel_name='coexistence_smoke_system_top',
            listen_port=SMOKE_GNMI_PORT_SYSTEM_TOP,
            paths=[system_top_level_xpath()],
            sample_interval='15s',
            duration_sec=duration_sec,
            attrs=(),
            populate_kwargs={},
            group_size=1,
        )
    )

    # SSIM3 ``test_09_components_top_level_subscription``: a single top-level subscription to
    # ``/components``. The per-type component set (PSU/FAN/TEMP/TRANSCEIVER/ASIC/CHASSIS/STORAGE/
    # CPU/LEAKAGE, with transceiver type/firmware metadata) is discovered here and cached so the
    # expected-leaf enumeration in ``build_smoke_component_expected_xpaths`` stays consistent with
    # the subscription window. Full SSIM3 components_xpath coverage is validated strictly per
    # timestamp from ``gnmi-series-component``.
    component_context = discover_components_top_level_context(dut)
    telemetryCache.add_data(_COMPONENT_CONTEXT_CACHE_KEY, component_context)
    jobs.append(
        GnmiCollectionJob(
            model='component',
            tunnel_name='coexistence_smoke_component_top',
            listen_port=SMOKE_GNMI_PORT_COMPONENT_TOP,
            paths=[components_top_level_xpath()],
            sample_interval='15s',
            duration_sec=duration_sec,
            attrs=(),
            populate_kwargs={},
            group_size=1,
        )
    )
    return jobs


def build_bgp_gnmi_jobs(dut, *, duration_sec: int = 120) -> List[GnmiCollectionJob]:
    """Build the BGP subscription used by coexistence reboot validation."""
    return [
        job
        for job in build_smoke_gnmi_jobs(dut, duration_sec=duration_sec)
        if job.model == "bgp"
    ]


def build_top_level_load_gnmi_jobs(
    dut, *, duration_sec: int = 150, is_nvos: bool
) -> List[GnmiCollectionJob]:
    """Interface + BGP sample subscriptions for SSIM coexistence smoke test15.

    NGTS gnmic dial-out subscribes to the resolved supported paths for each model;
    this provides the same simultaneous interface/BGP subscription load as SSIM's
    top-level adapter while retaining per-path cache parsing.
    """
    jobs = build_interface_qos_gnmi_jobs(
        dut, duration_sec=duration_sec, is_nvos=is_nvos
    )
    jobs.extend(build_bgp_gnmi_jobs(dut, duration_sec=duration_sec))
    return jobs


def build_interface_qos_gnmi_jobs(
    dut, *, duration_sec: int = 120, is_nvos: bool
) -> List[GnmiCollectionJob]:
    lab = resolve_cumulus_lab_interfaces_on_dut(dut)
    iface_kwargs = {'interface': lab.test_iface}
    paths = resolve_model_xpaths('interface', INTERFACE_QOS_GNMI_ATTRS, iface_kwargs)
    jobs: List[GnmiCollectionJob] = []
    if paths:
        jobs.append(GnmiCollectionJob(
            model='interface',
            tunnel_name='coexistence_interface',
            listen_port=57421,
            paths=paths,
            sample_interval='5s',
            duration_sec=duration_sec,
            attrs=INTERFACE_QOS_GNMI_ATTRS,
            populate_kwargs=iface_kwargs,
            group_size=2,
        ))

    # NVOS declares the ``/qos`` prefixes in the UMF ``configured-prefixes.yaml`` but has no
    # external->internal mapping behind them, so the subscription is accepted and then streams
    # nothing. That silent acceptance never trips ``_gnmi_log_has_unsupported_yang_paths``, so an
    # NVOS run would hard-fail on an empty sample log instead of skipping.
    qos_prefixes = [] if is_nvos else generate_qos_container_xpaths(lab.test_iface)
    if qos_prefixes:
        jobs.append(GnmiCollectionJob(
            model='qos-prefix',
            tunnel_name='coexistence_qos_prefix',
            listen_port=57422,
            paths=qos_prefixes,
            sample_interval='15s',
            duration_sec=duration_sec,
        ))
    return jobs


ON_CHANGE_INTERFACE_ATTRS: Tuple[str, ...] = ('description',)
ON_CHANGE_INTERFACE_LISTEN_PORT = 57425
ON_CHANGE_INTERFACE_TUNNEL = 'coexistence_interface_onchange'
ON_CHANGE_INTERFACE_DURATION_SEC = 90
ON_CHANGE_INTERFACE_POST_CHANGE_WAIT_SEC = 15


def _remote_gnmi_on_change_log_path(tunnel_name: str, model: str) -> str:
    return '/tmp/%s_gnmi_%s_onchange.json' % (tunnel_name, model)


def _start_gnmic_on_change_subscription(
    collector: GrpcTunnelServer,
    *,
    model: str,
    paths: Sequence[str],
    duration_sec: int,
    gnmic_bin: str = 'gnmic',
) -> str:
    """Start gnmic ``stream-mode on-change`` subscription; return remote log path."""
    out_path = _remote_gnmi_on_change_log_path(collector.tunnel_name, model)
    _kill_stale_collector_gnmic(collector)
    paths_cli = ' '.join('--path %s' % (shlex.quote(p),) for p in paths)
    mtls_suffix = collector.gnmi_mtls_cli_suffix()
    inner = (
        '%s%s -d subscribe --mode stream --stream-mode on-change --encoding json %s '
        '> %s 2>&1'
    ) % (
        _collector_gnmic_base(collector, gnmic_bin),
        mtls_suffix,
        paths_cli,
        shlex.quote(out_path),
    )
    start_cmd = (
        'OUT=%s; rm -f "$OUT"; nohup timeout --foreground %d bash -c %s >/dev/null 2>&1 &'
    ) % (shlex.quote(out_path), int(duration_sec), shlex.quote(inner))
    assert collector.server is not None
    EngineAdapterTool.run_cmd(collector.server, start_cmd, timeout=60)
    _assert_collector_gnmic_listener(collector, timeout_sec=25)
    return out_path


def _set_interface_description(dut, interface: str, description: str) -> None:
    with allure.step('Set interface %s description=%r' % (interface, description)):
        Port(name=interface).interface.set(
            NvosConst.DESCRIPTION,
            description,
            apply=True,
            ask_for_confirmation=True,
            dut_engine=dut,
        ).verify_result()


def _clear_interface_description(dut, interface: str) -> None:
    with allure.step('Clear interface %s description' % (interface,)):
        Port(name=interface).interface.unset(
            op_param=NvosConst.DESCRIPTION,
            apply=True,
            ask_for_confirmation=True,
            dut_engine=dut,
        ).verify_result()


def _scalar_for_leaf(entry: Any, leaf: str) -> Optional[Any]:
    """Scalar for ``leaf`` inside a gNMI update.

    json_ietf sometimes stores the whole component ``state`` object on an update
    whose path ends in one leaf. The first scalar in that object is often
    ``mfg-name`` (``DELTA``) while ``model-name`` is the part string. Prefer the
    value whose key is ``leaf``; fall back to the only scalar.
    """
    target = leaf.strip().lower()
    keyed: List[Any] = []
    scalars: List[Any] = []

    def _walk(node: Any, key: Optional[str] = None) -> None:
        if isinstance(node, dict):
            for child_key, child in node.items():
                if child_key == 'xpath':
                    continue
                _walk(child, str(child_key))
            return
        if isinstance(node, (list, tuple, set)):
            for item in node:
                _walk(item, key)
            return
        if node is None:
            return
        scalars.append(node)
        if not key:
            return
        norm = key.split(':')[-1].strip('/').split('/')[-1].lower()
        if norm == target:
            keyed.append(node)

    _walk(entry)
    if keyed:
        return keyed[-1]
    return scalars[0] if len(scalars) == 1 else None


def _entry_leaf_values(entry: Any) -> List[Any]:
    """Flatten leaf values of a parsed gNMI entry.

    :func:`_extract_updates` merges the gnmic ``values`` dict alongside ``xpath``
    (there is no single ``value`` key), so collect every non-``xpath`` leaf.
    """
    values: List[Any] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == 'xpath':
                    continue
                _walk(val)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                _walk(item)
        elif node is not None:
            values.append(node)

    _walk(entry)
    return values


def _extract_on_change_values(series: Dict[str, Any], *, xpath_substr: str) -> List[Any]:
    """Collect update values whose xpath contains *xpath_substr*."""
    values: List[Any] = []
    needle = xpath_substr.lstrip('/')
    for _ts, entries in (series or {}).items():
        if not isinstance(entries, dict):
            continue
        for key, entry in entries.items():
            xpath = key if isinstance(key, str) else ''
            if isinstance(entry, dict):
                xpath = str(entry.get('xpath') or xpath)
            if needle not in xpath.lstrip('/'):
                continue
            values.extend(_entry_leaf_values(entry))
    return values


def run_and_validate_interface_on_change_subscription(
    dut,
    sonic_mgmt,
    *,
    interface: str,
    duration_sec: int = ON_CHANGE_INTERFACE_DURATION_SEC,
    is_nvos: bool = False,
) -> None:
    """SSIM ``test_13_on_change_subscription_interface`` — dial-out on-change for description."""
    iface_kwargs = {'interface': interface}
    paths = resolve_model_xpaths('interface', ON_CHANGE_INTERFACE_ATTRS, iface_kwargs)
    assert paths, 'No on-change interface xpaths resolved for %r' % (interface,)

    job = GnmiCollectionJob(
        model='interface-onchange',
        tunnel_name=ON_CHANGE_INTERFACE_TUNNEL,
        listen_port=ON_CHANGE_INTERFACE_LISTEN_PORT,
        paths=paths,
        sample_interval='0s',
        duration_sec=duration_sec,
        attrs=ON_CHANGE_INTERFACE_ATTRS,
        populate_kwargs=iface_kwargs,
        group_size=1,
    )
    new_description = 'coex-onchange-%d' % (int(time.time()),)
    use_mtls = not is_nvos
    session = setup_gnmi_coexistence_session(
        dut, sonic_mgmt, [job], use_mtls=use_mtls, is_nvos=is_nvos
    )
    out_path = ''
    try:
        with allure.step('Start gNMI on-change subscription for interface description'):
            out_path = _start_gnmic_on_change_subscription(
                session.setups[0].collector,
                model=job.model,
                paths=job.paths,
                duration_sec=duration_sec,
                gnmic_bin=session.gnmic_bin,
            )
        from ngts.tests_nvos.system.telemetry.otel.cumulus.gnmi_dialout_mtls import (
            GNMI_DIALOUT_RETRY_INTERVAL,
        )

        tunnel_connect_timeout = max(120, GNMI_DIALOUT_RETRY_INTERVAL * 3 + 20)
        with allure.step('Restart DUT grpc-tunnel client for on-change subscription'):
            _restart_dut_grpc_tunnel(dut, job.tunnel_name, is_nvos=is_nvos)
            time.sleep(2)
            _assert_grpc_tunnel_connected(
                dut,
                job.tunnel_name,
                listen_port=job.listen_port,
                timeout_sec=tunnel_connect_timeout,
                sonic_mgmt=sonic_mgmt,
                gnmic_log_paths=[out_path],
            )

        # Allow sync response before triggering the change.
        time.sleep(5)
        _set_interface_description(dut, interface, new_description)
        time.sleep(ON_CHANGE_INTERFACE_POST_CHANGE_WAIT_SEC)

        with allure.step('Parse on-change gnmic log into gnmi_on_change cache'):
            _wait_for_remote_file(sonic_mgmt, out_path, timeout_sec=45)
            log_content = _read_remote_file(sonic_mgmt, out_path)
            if _gnmi_log_has_unsupported_yang_paths(log_content):
                pytest.skip(
                    'Interface description on-change path unsupported on DUT YANG (%s)'
                    % (out_path,)
                )
            series = parse_gnmic_log_to_series(log_content)
            if not series:
                pytest.fail(
                    'gNMI on-change produced no timestamps (%s); tail=%s'
                    % (out_path, (log_content or '')[-800:])
                )
            telemetryCache.add_data('gnmi_on_change-series-interface', series)
            allure.attach(
                'timestamps=%d description=%r' % (len(series), new_description),
                'on_change_interface_summary',
            )

        with allure.step('Validate on-change updates include the new interface description'):
            values = _extract_on_change_values(series, xpath_substr='description')
            allure.attach(str(values), 'on_change_description_values')
            assert values, (
                'No on-change updates for interface description; series timestamps=%s'
                % (sorted(series.keys()),)
            )
            normalized = {str(v).strip().strip('"') for v in values if v is not None}
            assert new_description in normalized, (
                'Expected description %r in on-change updates; got %r'
                % (new_description, sorted(normalized))
            )
    finally:
        try:
            _clear_interface_description(dut, interface)
        except Exception as exc:  # noqa: BLE001
            logger.warning('Failed to clear interface description: %s', exc)
        teardown_gnmi_coexistence_session(dut, session)


def build_component_system_gnmi_jobs(dut, *, duration_sec: int = 120) -> List[GnmiCollectionJob]:
    component_name = discover_storage_component_name(dut)
    component_kwargs = {'component_name': component_name}
    system_paths = resolve_model_xpaths('system', SYSTEM_GNMI_ATTRS, {})
    component_paths = resolve_model_xpaths('component', COMPONENT_GNMI_ATTRS, component_kwargs)
    cpu_kwargs = {'component_name': CPU_COMPONENT_NAME}
    cpu_paths = resolve_model_xpaths('component', CPU_PLATFORM_HW_GNMI_ATTRS, cpu_kwargs)
    psu_name = discover_psu_component_name(dut)
    psu_kwargs = {'component_name': psu_name} if psu_name else {}
    psu_paths = (
        ['/components/component/state/%s' % (leaf,) for leaf in PSU_STATE_LEAF_BY_ATTR.values()] +
        ['/components/component/state/mfg-name']
        if psu_name
        else []
    )
    jobs: List[GnmiCollectionJob] = []
    if system_paths:
        jobs.append(
            GnmiCollectionJob(
                model='system',
                tunnel_name='coexistence_system',
                listen_port=57431,
                paths=system_paths,
                sample_interval='5s',
                duration_sec=duration_sec,
                attrs=SYSTEM_GNMI_ATTRS,
                populate_kwargs={'mount_point': '/'},
                group_size=2,
            )
        )
    if component_paths:
        jobs.append(
            GnmiCollectionJob(
                model='component',
                tunnel_name='coexistence_component',
                listen_port=57432,
                paths=component_paths,
                sample_interval='5s',
                duration_sec=duration_sec,
                attrs=COMPONENT_GNMI_ATTRS,
                populate_kwargs=component_kwargs,
                group_size=2,
            )
        )
    if cpu_paths:
        jobs.append(
            GnmiCollectionJob(
                model=CPU_COMPONENT_GNMI_MODEL,
                tunnel_name='coexistence_component_cpu',
                listen_port=CPU_COMPONENT_GNMI_LISTEN_PORT,
                paths=cpu_paths,
                sample_interval='5s',
                duration_sec=duration_sec,
                attrs=CPU_PLATFORM_HW_GNMI_ATTRS,
                populate_kwargs=cpu_kwargs,
                group_size=1,
                encoding=CPU_COMPONENT_GNMI_ENCODING,
            )
        )
    if psu_paths:
        jobs.append(
            GnmiCollectionJob(
                model=PSU_COMPONENT_GNMI_MODEL,
                tunnel_name='coexistence_component_psu',
                listen_port=PSU_COMPONENT_GNMI_LISTEN_PORT,
                paths=psu_paths,
                sample_interval='5s',
                duration_sec=duration_sec,
                attrs=PSU_PLATFORM_HW_GNMI_ATTRS,
                populate_kwargs=psu_kwargs,
                group_size=1,
                encoding=PSU_COMPONENT_GNMI_ENCODING,
                optional=True,
            )
        )
    return jobs
