"""NVOS-only: rebuild the OTLP export from Redis and compare it to what the collector got.

The switch's ``agent-mappings.yaml`` tells, for every metric, which Redis DB, table,
key and field (or calculator / builder) nv-umf-agent reads. This module re-reads
the same Redis rows and re-applies the same transforms, so every exported
datapoint can be checked against its source:

* :class:`RedisSnapshotReader` takes a batched snapshot of the needed keys (one Lua
  ``EVAL`` per DB, via :class:`~ngts.nvos_tools.infra.Tools.Tools.DatabaseTool`) and
  records the DUT clock at the time of each read.
* :func:`build_interface_mappings` ports UMF ``interface_mappings.go`` (alias <->
  internal name <-> OID, plane ports, peer ports).
* :func:`collector_rows` ports each UMF collector's key rewrite (alias for
  interface tables, module name for transceivers, display name for fans, ...).
* :func:`build_expected_points` ports ``BuildOTLPMetric`` over those rows, with the
  calculators / builders from :mod:`umf_calculators`.
* :func:`compare_metric` applies Cumulus-style checks: counters must sit between the
  pre and post snapshot, moving gauges within a percentage, states exactly - plus
  identity-based series pairing and descriptive-label comparison.

Collector selection, key rewrites and label rules follow UMF ``contractagent`` at
:data:`umf_calculators.UMF_SOURCE_COMMIT`.
"""

import fnmatch
import logging
import math
import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.system.telemetry.otel.constants import OtelDbValidationConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.validations import OtelDataValidations
from ngts.tests_nvos.system.telemetry.otel.umf_calculators import (
    HISTOGRAM_BUILDERS,
    BuilderError,
    CalculatorContext,
    go_parse_float,
    go_parse_int,
    lookup_builder,
    lookup_calculator,
)

logger = logging.getLogger(__name__)

Attrs = Dict[str, str]
LabelKey = Tuple[Tuple[str, str], ...]
ReadItem = Tuple[str, Optional[List[str]]]

_GLOB_CHARS_RE = re.compile(r"[*?\[]")
_TIME_PARTS = 2
# Keys per EVAL call. A single command covering hundreds of keys (each with many
# fields, e.g. a full-hash CONFIG_DB read) can return tens of KB in one shot, which
# the SSH transport underneath ``engine.run_cmd`` has been observed to truncate;
# chunking keeps each command's output modest.
_MAX_KEYS_PER_EVAL = 100
_MISSING_ROW = "<missing>"  # Lua marker for a requested key that is not in Redis


# --------------------------------------------------------------------------- #
# Redis access
# --------------------------------------------------------------------------- #


def _db_key(db: str, table: str, key: str) -> str:
    return f"{table}{OtelDbValidationConst.DB_KEY_SEPARATOR[db]}{key}"


@dataclass
class RedisSnapshot:
    """Hash rows read from Redis, keyed by DB name then full Redis key."""

    label: str
    raw: Dict[str, Dict[str, Attrs]]
    t_before_ns: int
    t_after_ns: int
    _tables: Dict[Tuple[str, str], Dict[str, Attrs]] = field(default_factory=dict)

    def table(self, db: str, table: str) -> Dict[str, Attrs]:
        """Rows of ``db``/``table`` keyed by the key after ``TABLE<sep>``."""
        cache_key = (db, table)
        if cache_key not in self._tables:
            prefix = f"{table}{OtelDbValidationConst.DB_KEY_SEPARATOR[db]}"
            rows: Dict[str, Attrs] = {
                full_key[len(prefix):]: dict(attrs)
                for full_key, attrs in self.raw.get(db, {}).items()
                if full_key.startswith(prefix)
            }
            self._tables[cache_key] = rows
        return self._tables[cache_key]

    def hash(self, db: str, full_key: str) -> Attrs:
        return dict(self.raw.get(db, {}).get(full_key, {}))

    def hash_len(self, db: str, table: str, key: str) -> int:
        return len(self.raw.get(db, {}).get(_db_key(db, table, key), {}))

    def merged(self, other: "RedisSnapshot") -> "RedisSnapshot":
        raw = {db: dict(rows) for db, rows in self.raw.items()}
        for db, rows in other.raw.items():
            raw.setdefault(db, {}).update(rows)
        return RedisSnapshot(self.label, raw, self.t_before_ns, other.t_after_ns)


class RedisSnapshotReader:
    """Batched Redis reads through ``Tools.DatabaseTool``: one atomic ``EVAL`` per DB.

    Keys are listed once per DB (``sonic_db_cli_get_keys``) and glob patterns are
    matched locally with :mod:`fnmatch`; literal keys skip the listing. Every DB's
    ``EVAL`` starts with ``TIME``, so the snapshot timestamps are the switch clock.
    """

    def __init__(self, dut):
        self.dut = dut

    def read(
        self,
        requests: Dict[str, List[ReadItem]],
        label: str,
        allow_empty: bool = False,
    ) -> RedisSnapshot:
        """Read ``requests``. ``allow_empty`` returns an empty snapshot (timestamps 0) when no key
        resolves; leave it off for callers that compare against OTLP timestamps."""
        requests = {db: items for db, items in requests.items() if items}
        unknown = sorted(set(requests) - set(OtelDbValidationConst.DB_KEY_SEPARATOR))
        if unknown:
            raise ValueError(f"Redis DBs with no known key separator: {unknown}")

        key_cache: Dict[str, List[str]] = {}
        raw: Dict[str, Dict[str, Attrs]] = {}
        t_before_ns: Optional[int] = None
        t_after_ns = 0
        for db in sorted(requests):
            keys = sorted({
                key for pattern, _ in requests[db] for key in self._match_keys(db, pattern, key_cache)
            })
            if not keys:
                raw[db] = {}
                continue
            fields = self._merged_fields(requests[db])
            script = self._snapshot_script(fields)
            db_raw: Dict[str, Attrs] = {}
            for offset in range(0, len(keys), _MAX_KEYS_PER_EVAL):
                chunk = keys[offset:offset + _MAX_KEYS_PER_EVAL]
                output = Tools.DatabaseTool.sonic_db_cli_eval(
                    engine=self.dut,
                    asic="",
                    db_name=db,
                    script=script,
                    keys=chunk,
                )
                chunk_raw, ts_ns = self._parse(output, db, label, chunk)
                db_raw.update(chunk_raw)
                t_before_ns = ts_ns if t_before_ns is None else t_before_ns
                t_after_ns = ts_ns
            raw[db] = db_raw
        if t_before_ns is None and allow_empty:
            t_before_ns = 0
        if t_before_ns is None:
            raise AssertionError(
                f"Redis snapshot '{label}': no DB in {sorted(requests)} resolved any key - "
                "no switch clock reading, would stamp the snapshot at epoch 0."
            )
        snapshot = RedisSnapshot(label, raw, t_before_ns, t_after_ns)
        logger.info(
            "Redis snapshot '%s': %s",
            label,
            ", ".join(f"{db}={len(rows)} keys" for db, rows in raw.items()),
        )
        return snapshot

    def _match_keys(self, db: str, pattern: str, key_cache: Dict[str, List[str]]) -> List[str]:
        if not _GLOB_CHARS_RE.search(pattern):
            return [pattern]
        if db not in key_cache:
            raw = Tools.DatabaseTool.sonic_db_cli_get_keys(engine=self.dut, asic="", db_name=db)
            key_cache[db] = [line.strip().strip('"') for line in (raw or "").splitlines() if line.strip()]
        return [key for key in key_cache[db] if fnmatch.fnmatch(key, pattern)]

    @staticmethod
    def _merged_fields(items: List[ReadItem]) -> Optional[List[str]]:
        """``None`` (whole-hash ``HGETALL``) if any item wants it, else the union of every field list."""
        if any(fields is None for _, fields in items):
            return None
        merged: List[str] = []
        for _, fields in items:
            for name in fields:
                if name not in merged:
                    merged.append(name)
        return merged

    @staticmethod
    def _snapshot_script(fields: Optional[List[str]]) -> str:
        """Quote-free Lua: ``TIME`` first, then one tab-joined row per key (``HMGET``/``HGETALL``)."""
        if fields:
            field_list = ",".join(f"'{name}'" for name in fields)
            row_body = (
                f"local fields={{{field_list}}} local vals=redis.call('HMGET',k,unpack(fields)) "
                "for j,name in ipairs(fields) do if vals[j] then row=row..'\\t'..name..'\\t'..vals[j] end end"
            )
        else:
            row_body = (
                "local flat=redis.call('HGETALL',k) "
                "for j=1,#flat do row=row..'\\t'..flat[j] end"
            )
        return (
            "local t=redis.call('TIME') local out={t[1]..' '..t[2]} "
            "for i=1,#KEYS do local k=KEYS[i] local row=k "
            f"if redis.call('EXISTS',k)==0 then row=row..'\\t{_MISSING_ROW}' else {row_body} end "
            "out[#out+1]=row end "
            "return table.concat(out,'\\n')"
        )

    @staticmethod
    def _parse(output: str, db: str, label: str, keys: List[str]) -> Tuple[Dict[str, Attrs], int]:
        text = (output or "").strip("\n")
        # sonic-db-cli, unlike `redis-cli --raw`, quotes a string reply like `redis-cli`'s
        # default (non-raw) mode; the EVAL script's own embedded newlines stay literal.
        # Stripped independently (not only as a matched pair) since a truncated transport
        # read can drop the closing quote while leaving the opening one.
        if text.startswith('"'):
            text = text[1:]
        if text.endswith('"'):
            text = text[:-1]
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise AssertionError(f"Redis snapshot '{label}' {db}: empty EVAL output")
        time_parts = lines[0].split()
        if len(time_parts) != _TIME_PARTS:
            raise AssertionError(f"Redis snapshot '{label}' {db}: bad TIME line {lines[0]!r}")
        seconds, micros = time_parts
        ts_ns = int(seconds) * 1_000_000_000 + int(micros) * 1_000
        if len(lines) - 1 != len(keys):
            raise AssertionError(f"Redis snapshot '{label}' {db}: got {len(lines) - 1} rows for {len(keys)} keys")
        rows: Dict[str, Attrs] = {}
        for line in lines[1:]:
            parts = line.split("\t")
            key = parts[0].strip().strip("'\"")
            fields = parts[1:]
            if fields == [_MISSING_ROW]:
                continue
            if len(fields) % 2:
                raise AssertionError(f"Redis snapshot '{label}' {db}: incomplete field pair in row {key!r}")
            rows[key] = dict(zip(fields[::2], fields[1::2]))
        return rows, ts_ns


# --------------------------------------------------------------------------- #
# interface_mappings.go
# --------------------------------------------------------------------------- #

_PLANE_PORT_ALIAS_RE = re.compile(r"^sw.+pl\d+$", re.IGNORECASE)
_PLANE_INTERNAL_SUFFIX_RE = re.compile(r"pl\d+$", re.IGNORECASE)
_SWITCH_ALIAS_RE = re.compile(r"^(sw[A-Za-z]*\d+)")


@dataclass
class InterfaceMappings:
    alias_to_internal: Dict[str, str] = field(default_factory=dict)
    internal_to_alias: Dict[str, str] = field(default_factory=dict)
    alias_to_oid: Dict[str, str] = field(default_factory=dict)
    oid_to_alias: Dict[str, str] = field(default_factory=dict)

    def _add(self, alias: str, internal: str) -> None:
        if not alias or not internal or alias in self.alias_to_internal:
            return
        self.alias_to_internal[alias] = internal
        self.internal_to_alias[internal] = alias

    def is_peer_entry(self, redis_key: str, name: str) -> bool:
        return (
            self.alias_to_oid.get(name) == redis_key and
            self.alias_to_internal.get(name) == name
        )

    def peer_names(self) -> List[str]:
        return [name for key, name in self.oid_to_alias.items() if self.is_peer_entry(key, name)]

    def switch_ports(self) -> Dict[str, str]:
        """``{alias: oid}`` for local switch ports (peer entries excluded)."""
        return {
            alias: oid
            for oid, alias in self.oid_to_alias.items()
            if not self.is_peer_entry(oid, alias)
        }


def _plane_parent(name: str) -> str:
    match = _PLANE_INTERNAL_SUFFIX_RE.search(name)
    return name[: match.start()] if match else name


def _merge_state_plane_ports(maps: InterfaceMappings, state_rows: Dict[str, Attrs]) -> None:
    """``mergePlanePortAliasesFromStateIbPort``."""
    state_alias_to_internal = {
        attrs["alias"]: internal for internal, attrs in state_rows.items() if attrs.get("alias")
    }
    for alias, internal in state_alias_to_internal.items():
        if _PLANE_PORT_ALIAS_RE.match(alias):
            maps._add(alias, internal)
    for internal, attrs in state_rows.items():
        if not attrs or attrs.get("alias") or not _PLANE_INTERNAL_SUFFIX_RE.search(internal):
            continue
        parent = _plane_parent(internal)
        suffix = internal[len(parent):]
        parent_alias = maps.internal_to_alias.get(parent, "")
        derived = parent_alias + suffix
        is_plane_child = parent != internal and suffix.lower().startswith("pl")
        if is_plane_child and parent_alias and _PLANE_PORT_ALIAS_RE.match(derived):
            maps._add(derived, internal)
    for alias in list(maps.alias_to_internal):
        parent_alias = _plane_parent(alias) if _PLANE_PORT_ALIAS_RE.match(alias) else alias
        if parent_alias == alias or parent_alias in maps.alias_to_internal:
            continue
        if parent_alias in state_alias_to_internal:
            maps._add(parent_alias, state_alias_to_internal[parent_alias])


def _merge_peer_ports(maps: InterfaceMappings, snapshot: RedisSnapshot) -> None:
    """``generatePeerAliasMappings``: the gNMI name is ``peer_port_name`` or the row key."""
    for redis_key, attrs in snapshot.table("COUNTERS_DB", "PEER_COUNTERS").items():
        if snapshot.hash_len("COUNTERS_DB", "PEER_COUNTERS", redis_key) == 0 and ":" in redis_key:
            continue
        name = attrs.get("peer_port_name", "").strip() or redis_key
        existing = maps.alias_to_oid.get(name)
        if existing is not None and existing != redis_key:
            continue
        maps.alias_to_internal[name] = name
        maps.internal_to_alias[name] = name
        maps.alias_to_oid[name] = redis_key
        maps.oid_to_alias[redis_key] = name


def build_interface_mappings(snapshot: RedisSnapshot) -> InterfaceMappings:
    """Port of ``UpdateInterfaceMaps`` over a discovery snapshot."""
    maps = InterfaceMappings()
    for internal, attrs in snapshot.table("CONFIG_DB", "IB_PORT").items():
        alias = attrs.get("alias", "")
        if alias:
            maps.alias_to_internal[alias] = internal
            maps.internal_to_alias[internal] = alias
    for alias, attrs in snapshot.table("APPL_DB", "ALIAS_PORT_MAP").items():
        maps._add(alias, attrs.get("name", ""))
    _merge_state_plane_ports(maps, snapshot.table("STATE_DB", "IB_PORT_TABLE"))

    name_map = snapshot.hash("COUNTERS_DB", "COUNTERS_PORT_NAME_MAP")
    for internal, alias in list(maps.internal_to_alias.items()):
        oid = name_map.get(internal, "")
        if oid:
            maps.oid_to_alias[oid] = alias
            maps.alias_to_oid[alias] = oid
    _merge_peer_ports(maps, snapshot)
    return maps


def extract_switch_from_alias(alias: str) -> str:
    """``extractSwitchFromAliasTable``: ``sw1p1`` -> ``sw1`` (transceiver module name)."""
    match = _SWITCH_ALIAS_RE.match(alias)
    return match.group(1) if match else alias


# --------------------------------------------------------------------------- #
# Port / entity selection
# --------------------------------------------------------------------------- #


@dataclass
class PortSelection:
    ports: List[str]
    oper_status: Dict[str, str]
    speeds: Dict[str, str]
    peers: Dict[str, str]
    modules: Set[str]
    seed: int

    def describe(self) -> str:
        lines = [f"random seed: {self.seed}", "switch ports:"]
        for alias in self.ports:
            lines.append(
                f"  {alias}: oper_status={self.oper_status.get(alias, '?')} "
                f"speed={self.speeds.get(alias, '?')}"
            )
        lines.append(f"peer ports: {sorted(self.peers) or '(none)'}")
        lines.append(f"transceiver modules: {sorted(self.modules) or '(none)'}")
        return "\n".join(lines)


def discovery_requests() -> Dict[str, List[ReadItem]]:
    """Rows UMF reads to build its interface mappings (plus peer-to-switch-port links)."""
    def glob(db: str, table: str) -> str:
        return _db_key(db, table, "*")

    return {
        "CONFIG_DB": [(glob("CONFIG_DB", "IB_PORT"), ["alias"])],
        "APPL_DB": [(glob("APPL_DB", "ALIAS_PORT_MAP"), ["name"])],
        "STATE_DB": [(glob("STATE_DB", "IB_PORT_TABLE"), ["alias", "oper_status"])],
        "COUNTERS_DB": [
            ("COUNTERS_PORT_NAME_MAP", None),
            (glob("COUNTERS_DB", "PEER_COUNTERS"), ["peer_port_name"]),
            (glob("COUNTERS_DB", "PEER_PORT_MAPPING"), ["switch_port_alias"]),
        ],
    }


def speed_requests(maps: InterfaceMappings) -> Dict[str, List[ReadItem]]:
    return {
        "COUNTERS_DB": [
            (_db_key("COUNTERS_DB", "COUNTERS", oid), [OtelDbValidationConst.PORT_SPEED_FIELD])
            for oid in maps.switch_ports().values()
        ]
    }


def select_ports(
    maps: InterfaceMappings,
    discovery: RedisSnapshot,
    *,
    seed: Optional[int] = None,
) -> PortSelection:
    """Pick random up ports (at least one per operational speed), random down ports, their peers."""
    seed = seed if seed is not None else random.SystemRandom().randrange(1 << 30)
    rng = random.Random(seed)
    state_rows = discovery.table("STATE_DB", "IB_PORT_TABLE")
    counters_rows = discovery.table("COUNTERS_DB", "COUNTERS")

    oper: Dict[str, str] = {}
    speeds: Dict[str, str] = {}
    for alias, oid in maps.switch_ports().items():
        internal = maps.alias_to_internal.get(alias, "")
        if internal not in state_rows:
            continue
        oper[alias] = state_rows[internal].get("oper_status", "").strip().lower()
        speeds[alias] = counters_rows.get(oid, {}).get(OtelDbValidationConst.PORT_SPEED_FIELD, "")

    up = sorted(alias for alias, status in oper.items() if status == "up")
    down = sorted(alias for alias, status in oper.items() if status != "up")

    chosen: List[str] = []
    by_speed: Dict[str, List[str]] = {}
    for alias in up:
        by_speed.setdefault(speeds.get(alias, ""), []).append(alias)
    for speed in sorted(by_speed):
        chosen.append(rng.choice(by_speed[speed]))
    remaining_up = [alias for alias in up if alias not in chosen]
    missing_up = max(0, OtelDbValidationConst.UP_PORT_COUNT - len(chosen))
    chosen += rng.sample(remaining_up, min(missing_up, len(remaining_up)))
    chosen += rng.sample(down, min(OtelDbValidationConst.DOWN_PORT_COUNT, len(down)))

    peer_map = discovery.table("COUNTERS_DB", "PEER_PORT_MAPPING")
    peer_names = set(maps.peer_names())
    peers = {
        name: key
        for key, attrs in peer_map.items()
        if (name := maps.oid_to_alias.get(key)) in peer_names and attrs.get("switch_port_alias") in chosen
    }
    if not peers and peer_names:
        for name in rng.sample(sorted(peer_names), min(OtelDbValidationConst.FALLBACK_PEER_COUNT, len(peer_names))):
            peers[name] = maps.alias_to_oid[name]

    return PortSelection(
        ports=chosen,
        oper_status={alias: oper[alias] for alias in chosen},
        speeds={alias: speeds.get(alias, "") for alias in chosen},
        peers=peers,
        modules={extract_switch_from_alias(alias) for alias in chosen},
        seed=seed,
    )


# --------------------------------------------------------------------------- #
# Mapping specs and UMF collector selection (contract/agent.go)
# --------------------------------------------------------------------------- #

_TRANSCEIVER_TABLES = ("TRANSCEIVER_STATUS", "TRANSCEIVER_INFO", "TRANSCEIVER_DOM_SENSOR", "TRANSCEIVER_DOM_THRESHOLD")
_CHANNEL_TABLES = ("TRANSCEIVER_STATUS", "TRANSCEIVER_DOM_SENSOR")
_PEER_TABLES = ("PEER_COUNTERS", "PEER_PORT_MAPPING")

KIND_CHANNEL = "channel-index"
KIND_EVENT = "event"
KIND_INTERFACE_ID = "interface-id"
KIND_INTERFACE_NAME = "interface-name"
KIND_FW_VERSION = "fw-version"
KIND_LEAKAGE = "leakage-info"
KIND_FAN = "fan-info"
KIND_LAST_REBOOT = "last-reboot"
KIND_PARTITION = "partition-info"
KIND_TABLE = "table"


@dataclass
class MetricSpec:
    name: str
    db: str
    table: str
    labels: Dict[str, Dict[str, Any]]
    value_info: Optional[Dict[str, Any]]
    is_int: bool
    metric_type: str
    builder: str
    key_regex: Optional["re.Pattern[str]"]
    default_key: str
    additional_tables: List[Tuple[str, str]]
    key_label: str
    kind: str
    collector_id: str
    unsupported: str = ""

    @property
    def is_histogram(self) -> bool:
        return self.metric_type == "histogram" or self.builder in HISTOGRAM_BUILDERS

    @property
    def value_pattern(self) -> Optional["re.Pattern[str]"]:
        if self.value_info and self.value_info.get("redisFieldPattern"):
            return re.compile(self.value_info["redisFieldPattern"])
        for info in self.labels.values():
            if info.get("redisFieldPattern"):
                return re.compile(info["redisFieldPattern"])
        return None

    @property
    def identity_labels(self) -> Set[str]:
        """Declared labels used to pair a Redis row with an OTEL series (see :func:`compare_metric`)."""
        identity_names = set(OtelDbValidationConst.IDENTITY_LABEL_NAMES)
        return {
            name for name, info in self.labels.items()
            if info.get("isKey") or info.get("extractedFromPattern") or name in identity_names
        }


_SINGLETON_COLLECTORS = {
    ("STATE_DB", "SYSTEM_COMPONENTS"): KIND_FW_VERSION,
    ("STATE_DB", "LEAKAGE_INFO"): KIND_LEAKAGE,
    ("STATE_DB", "FAN_INFO"): KIND_FAN,
    ("STATE_DB", "REBOOT_CAUSE"): KIND_LAST_REBOOT,
}


def _collector_kind(db: str, table: str, key_label: str, label_names: Iterable[str]) -> Tuple[str, str]:
    """``CollectorTypeRegistry`` order: first collector that claims the path wins.

    Returns ``(kind, collector id)``; metrics with the same id share one UMF collector.
    """
    if db == "STATE_DB" and table in _CHANNEL_TABLES and set(label_names) & {"channel", "lane"}:
        kind, collector_id = KIND_CHANNEL, KIND_CHANNEL
    elif db == "EVENT_DB" and table == "EVENT":
        kind, collector_id = KIND_EVENT, KIND_EVENT
    elif key_label == "interface":
        kind = KIND_INTERFACE_ID if db == "COUNTERS_DB" else KIND_INTERFACE_NAME
        collector_id = f"{kind}:{db}:{table}"
    elif (db, table) in _SINGLETON_COLLECTORS:
        kind = collector_id = _SINGLETON_COLLECTORS[(db, table)]
    elif db == "STATE_DB" and table == "SYSTEM" and key_label in ("partition", "slot"):
        kind, collector_id = KIND_PARTITION, KIND_PARTITION
    else:
        kind, collector_id = KIND_TABLE, f"{KIND_TABLE}:{db}:{table}:{key_label}"
    return kind, collector_id


def parse_metric_spec(name: str, mapping: Dict[str, Any]) -> MetricSpec:
    labels = {str(k): (v or {}) for k, v in (mapping.get("labels") or {}).items()}
    key_label = next((k for k, v in labels.items() if v.get("isKey")), "")
    default_key = str(mapping.get("defaultKey", "") or "")
    if default_key:
        key_label = "key_value"
    db = mapping.get("redisDB", "")
    table = mapping.get("redisTable", "")
    kind, collector_id = _collector_kind(db, table, key_label, labels)
    value_info = mapping.get("intValue") or mapping.get("floatValue")
    builder = mapping.get("metricBuilderFunc", "") or ""
    key_regex = mapping.get("keyRegex")

    unsupported = ""
    if kind == KIND_EVENT:
        unsupported = "event-driven metric (EVENT_DB), no steady-state Redis value"
    elif builder and lookup_builder(builder) is None:
        unsupported = f"metric builder {builder!r} is not ported"
    elif not builder and value_info is None:
        unsupported = "mapping has neither intValue/floatValue nor metricBuilderFunc"
    else:
        calcs = [info.get("calculatorFunc") for info in labels.values()]
        calcs.append((value_info or {}).get("calculatorFunc"))
        missing = sorted({c for c in calcs if c and lookup_calculator(c) is None})
        if missing:
            unsupported = f"calculator(s) {missing} are not ported"

    return MetricSpec(
        name=name,
        db=db,
        table=table,
        labels=labels,
        value_info=value_info,
        is_int=mapping.get("intValue") is not None,
        metric_type=str(mapping.get("metricType", "gauge") or "gauge").lower(),
        builder=builder,
        key_regex=re.compile(key_regex) if key_regex else None,
        default_key=default_key,
        additional_tables=[(t["redisDB"], t["redisTable"]) for t in mapping.get("additionalTables") or []],
        key_label=key_label,
        kind=kind,
        collector_id=collector_id,
        unsupported=unsupported,
    )


@dataclass
class CollectorGroup:
    """Metrics UMF serves from one collector instance (they share its key list and rows)."""

    kind: str
    collector_id: str
    db: str
    table: str
    keys: Set[str] = field(default_factory=set)
    additional_tables: List[Tuple[str, str]] = field(default_factory=list)
    specs: List[MetricSpec] = field(default_factory=list)


def group_collectors(specs: Iterable[MetricSpec]) -> Dict[str, CollectorGroup]:
    groups: Dict[str, CollectorGroup] = {}
    for spec in specs:
        group = groups.setdefault(
            spec.collector_id, CollectorGroup(spec.kind, spec.collector_id, spec.db, spec.table)
        )
        group.specs.append(spec)
        group.keys.add(spec.default_key or "*")
        for extra in spec.additional_tables:
            if extra not in group.additional_tables:
                group.additional_tables.append(extra)
    return groups


def _group_request_keys(
    group: CollectorGroup, maps: InterfaceMappings, selection: PortSelection
) -> List[Tuple[str, str, str]]:
    """``(db, table, key or glob)`` to read for one collector group."""
    tables = [(group.db, group.table)] + group.additional_tables
    if group.kind == KIND_CHANNEL:
        tables = [("STATE_DB", table) for table in _CHANNEL_TABLES]
    keys: Iterable[str]
    if group.kind == KIND_EVENT:
        keys = []
    elif group.kind == KIND_INTERFACE_ID and group.table == "COUNTERS":
        keys = [maps.alias_to_oid[a] for a in selection.ports if a in maps.alias_to_oid]
    elif group.kind == KIND_INTERFACE_ID:
        keys = selection.peers.values()
    elif group.kind == KIND_INTERFACE_NAME:
        internals = {maps.alias_to_internal[a] for a in selection.ports if a in maps.alias_to_internal}
        internals |= {i for i, a in maps.internal_to_alias.items() if a in selection.ports}
        keys = internals | set(selection.ports)
    elif group.kind == KIND_CHANNEL or (group.db == "STATE_DB" and group.table in _TRANSCEIVER_TABLES):
        keys = [f"{module}*" for module in selection.modules]
    elif group.kind == KIND_TABLE and "*" not in group.keys:
        keys = group.keys
    elif (group.db, group.table) == ("COUNTERS_DB", "COUNTERS"):
        tables, keys = tables[:1], [OtelDbValidationConst.SWITCH_OBJECT_OID_GLOB]
    else:
        keys = ["*"]
    if group.kind in (KIND_INTERFACE_ID, KIND_INTERFACE_NAME):
        tables = tables[:1]
    return [(db, table, key) for db, table in tables for key in keys]


def value_requests(
    groups: Dict[str, CollectorGroup],
    maps: InterfaceMappings,
    selection: PortSelection,
) -> Dict[str, List[ReadItem]]:
    """Redis keys to snapshot: the selected ports / peers / modules, whole tables for the rest."""
    requests: Dict[str, Set[str]] = {}
    for group in groups.values():
        for db, table, key in _group_request_keys(group, maps, selection):
            requests.setdefault(db, set()).add(_db_key(db, table, key))
    return {db: [(key, None) for key in sorted(keys)] for db, keys in requests.items()}


# --------------------------------------------------------------------------- #
# Collector rows (the data each UMF collector hands to BuildOTLPMetric)
# --------------------------------------------------------------------------- #


@dataclass
class Row:
    key: str
    attrs: Attrs


_TRANSCEIVER_STATUS_FIELD_RE = re.compile(r"(?P<prefix>(rx|tx)?)(?P<suffix>.+)(?P<index>\d+)")
_TRANSCEIVER_DOM_FIELD_RE = re.compile(r"(?P<prefix>(rx|tx)?)(?P<index>\d+)(?P<suffix>.+)")
_TRAILING_ID_RE = re.compile(r"(\d+)$")


_CHANNEL_FIELD_PATTERNS = (
    ("TRANSCEIVER_STATUS", _TRANSCEIVER_STATUS_FIELD_RE),
    ("TRANSCEIVER_DOM_SENSOR", _TRANSCEIVER_DOM_FIELD_RE),
)
_FW_TYPE_BY_PREFIX = (
    ("erot", "EROT"), ("bios", "BIOS"), ("bmc", "BMC"), ("fpga", "FPGA"), ("cpld", "CPLD"), ("sma", "SMA"),
)
_RAW_LANE_FIELD_PREFIXES = ("SAI_PORT_STAT_INFINIBAND_RAW_BER_STRING_LANE", "SAI_PORT_STAT_INFINIBAND_RAW_ERR_LANE")


def _channel_rows(group: CollectorGroup, snapshot: RedisSnapshot, maps: InterfaceMappings) -> List[Row]:
    groups: Dict[Tuple[str, str, str], Attrs] = {}
    for table, pattern in _CHANNEL_FIELD_PATTERNS:
        for key, attrs in snapshot.table("STATE_DB", table).items():
            module = extract_switch_from_alias(key)
            for leaf, value in attrs.items():
                match = pattern.search(leaf)
                if not match:
                    continue
                prefix, index, suffix = match.group("prefix"), match.group("index"), match.group("suffix")
                channel = groups.setdefault(
                    (module, index, prefix), {"name": module, "channel": index, "direction": prefix}
                )
                channel[suffix] = value
                channel[prefix + suffix] = value
    return [Row(k[0], attrs) for k, attrs in groups.items()]


def _fw_version_rows(group: CollectorGroup, snapshot: RedisSnapshot, maps: InterfaceMappings) -> List[Row]:
    rows = snapshot.table(group.db, group.table)
    out = []
    for key in sorted(rows):
        attrs = dict(rows[key])
        lower = key.lower()
        version_type, fw_id = "", "1"
        for prefix, label in _FW_TYPE_BY_PREFIX:
            if lower.startswith(prefix):
                version_type = label
                if label in ("CPLD", "SMA"):
                    match = _TRAILING_ID_RE.search(key)
                    fw_id = match.group(1) if match else ""
                break
        attrs.update({"name": key, "type": version_type, "id": fw_id})
        out.append(Row(key, attrs))
    return out


def _interface_id_rows(group: CollectorGroup, snapshot: RedisSnapshot, maps: InterfaceMappings) -> List[Row]:
    rows = snapshot.table(group.db, group.table)
    out = []
    if group.table == "COUNTERS":
        for oid, alias in maps.oid_to_alias.items():
            if maps.is_peer_entry(oid, alias) or not rows.get(oid):
                continue
            attrs = dict(rows[oid])
            attrs.update(OtelDbValidationConst.SWITCH_COUNTERS_CONSTANT_FIELDS)
            out.append(Row(alias, attrs))
    elif group.table in _PEER_TABLES:
        for name in maps.peer_names():
            if rows.get(maps.alias_to_oid[name]):
                out.append(Row(name, dict(rows[maps.alias_to_oid[name]])))
    return out


def _interface_name_rows(group: CollectorGroup, snapshot: RedisSnapshot, maps: InterfaceMappings) -> List[Row]:
    return [
        Row(maps.internal_to_alias.get(internal, internal), {k: v for k, v in attrs.items() if k != "alias"})
        for internal, attrs in snapshot.table(group.db, group.table).items()
    ]


def _leakage_rows(group: CollectorGroup, snapshot: RedisSnapshot, maps: InterfaceMappings) -> List[Row]:
    prefix = "LEAKAGE-"
    rows = snapshot.table(group.db, group.table)
    return [Row(k[len(prefix):] if k.startswith(prefix) else k, dict(a)) for k, a in rows.items()]


def _fan_rows(group: CollectorGroup, snapshot: RedisSnapshot, maps: InterfaceMappings) -> List[Row]:
    return [Row(a.get("display_name", k), dict(a)) for k, a in snapshot.table(group.db, group.table).items()]


def _last_reboot_rows(group: CollectorGroup, snapshot: RedisSnapshot, maps: InterfaceMappings) -> List[Row]:
    rows = snapshot.table(group.db, group.table)
    if not rows:
        return []
    latest = max(rows)
    attrs = dict(rows[latest])
    attrs["name"] = "CHASSIS"
    return [Row(latest, attrs)]


def _partition_rows(group: CollectorGroup, snapshot: RedisSnapshot, maps: InterfaceMappings) -> List[Row]:
    rows = snapshot.table(group.db, group.table)
    synthetic = {"build_id": "", "description": "", "disk": "", "release": ""}
    return [Row(k, dict(rows.get(k, synthetic))) for k in ("IMAGE1", "IMAGE2")]


def _table_rows(group: CollectorGroup, snapshot: RedisSnapshot, maps: InterfaceMappings) -> List[Row]:
    rows = snapshot.table(group.db, group.table)
    wanted = rows if "*" in group.keys else {k: v for k, v in rows.items() if k in group.keys}
    merged = {k: dict(v) for k, v in wanted.items()}
    for db, table in group.additional_tables:
        for key, attrs in snapshot.table(db, table).items():
            if key in merged:
                merged[key].update(attrs)
    transceiver = group.db == "STATE_DB" and group.table in _TRANSCEIVER_TABLES
    return [Row(extract_switch_from_alias(k) if transceiver else k, a) for k, a in merged.items()]


_ROW_BUILDERS = {
    KIND_INTERFACE_ID: _interface_id_rows,
    KIND_INTERFACE_NAME: _interface_name_rows,
    KIND_CHANNEL: _channel_rows,
    KIND_FW_VERSION: _fw_version_rows,
    KIND_LEAKAGE: _leakage_rows,
    KIND_FAN: _fan_rows,
    KIND_LAST_REBOOT: _last_reboot_rows,
    KIND_PARTITION: _partition_rows,
    KIND_TABLE: _table_rows,
}


def collector_rows(group: CollectorGroup, snapshot: RedisSnapshot, maps: InterfaceMappings) -> List[Row]:
    """The ``[]RedisTableData`` a UMF collector of ``group.kind`` would build from ``snapshot``."""
    builder = _ROW_BUILDERS.get(group.kind)
    return builder(group, snapshot, maps) if builder else []


# --------------------------------------------------------------------------- #
# BuildOTLPMetric port
# --------------------------------------------------------------------------- #


@dataclass
class ExpectedPoint:
    labels: Dict[str, str]
    value: Any
    row_key: str


@dataclass
class ExpectedSet:
    points: List[ExpectedPoint] = field(default_factory=list)
    error_rows: Set[str] = field(default_factory=set)


class _SkipRow(Exception):
    """The Go code returns an error for this row (datapoint skipped)."""


def _is_pattern_site(info: Dict[str, Any]) -> bool:
    return bool(info.get("extractedFromPattern") or info.get("redisFieldPattern"))


def _labels_present(spec: MetricSpec, attrs: Attrs, *, skip_pattern_sites: bool) -> bool:
    for info in spec.labels.values():
        if skip_pattern_sites and _is_pattern_site(info):
            continue
        if info.get("calculatorFunc") or _is_pattern_site(info) or info.get("isKey") or _const_label(info):
            continue
        field_name = info.get("redisField")
        if field_name and field_name not in attrs and not info.get("optional"):
            return False
    return True


def _const_label(info: Dict[str, Any]) -> str:
    value = info.get("constantValue")
    return "" if value is None else str(value)


def _apply_labels(
    spec: MetricSpec, key: str, attrs: Attrs, ctx: CalculatorContext, *, skip_pattern_sites: bool
) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    for name, info in spec.labels.items():
        if skip_pattern_sites and _is_pattern_site(info):
            continue
        calc = info.get("calculatorFunc")
        if calc:
            result = lookup_calculator(calc)(key, attrs, ctx)
            labels[name] = result if isinstance(result, str) else ""
        elif _is_pattern_site(info):
            continue
        elif info.get("isKey"):
            labels[name] = key
        elif info.get("redisField"):
            if info["redisField"] in attrs:
                labels[name] = attrs[info["redisField"]]
            elif info.get("optional"):
                labels[name] = str(info.get("defaultValue", "") or "")
        else:
            labels[name] = _const_label(info)
    return labels


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_value(spec: MetricSpec, raw: str) -> Any:
    try:
        return go_parse_int(raw) if spec.is_int else go_parse_float(raw)
    except ValueError as err:
        raise _SkipRow(str(err)) from err


def _row_value(spec: MetricSpec, key: str, attrs: Attrs, ctx: CalculatorContext) -> Any:
    info = spec.value_info or {}
    if info.get("calculatorFunc"):
        return lookup_calculator(info["calculatorFunc"])(key, attrs, ctx)
    if info.get("redisField"):
        if info["redisField"] not in attrs:
            raise _SkipRow(f"field {info['redisField']} missing")
        return _parse_value(spec, attrs[info["redisField"]])
    if info.get("constantValue") is not None:
        constant = int(info["constantValue"])
        return constant if spec.is_int else float(constant)
    raise _SkipRow("no value source")


def _pattern_points(spec: MetricSpec, row: Row, ctx: CalculatorContext) -> List[ExpectedPoint]:
    pattern = spec.value_pattern
    index_to_value: Dict[str, str] = {}
    for field_name, field_value in row.attrs.items():
        match = pattern.search(field_name) if pattern else None
        if not match or match.lastindex is None:
            continue
        captured = match.group(1)
        if field_name.startswith(_RAW_LANE_FIELD_PREFIXES):
            try:
                captured = str(go_parse_int(captured) + 1)
            except ValueError:
                continue
        index_to_value[captured] = field_value
    if not index_to_value or not _labels_present(spec, row.attrs, skip_pattern_sites=True):
        return []

    index_label = next((n for n, i in spec.labels.items() if i.get("extractedFromPattern")), "")
    value_sites = [n for n, i in spec.labels.items() if i.get("redisFieldPattern")]
    constant = (spec.value_info or {}).get("constantValue")
    points = []
    for index, field_value in index_to_value.items():
        if constant is not None:
            value = int(constant) if spec.is_int else float(constant)
        else:
            value = _parse_value(spec, field_value)
        labels = _apply_labels(spec, row.key, row.attrs, ctx, skip_pattern_sites=True)
        for name in value_sites:
            labels[name] = field_value
        if index_label:
            labels[index_label] = index
        points.append(ExpectedPoint(labels, value, row.key))
    return points


def build_expected_points(spec: MetricSpec, rows: List[Row], ctx: CalculatorContext) -> ExpectedSet:
    """``BuildOTLPMetric`` for one metric over its collector's rows."""
    result = ExpectedSet()
    builder = lookup_builder(spec.builder) if spec.builder else None
    for row in rows:
        if spec.key_regex is not None and not spec.key_regex.search(row.key):
            continue
        try:
            if builder is not None:
                for labels, value in builder(row.key, row.attrs):
                    result.points.append(ExpectedPoint(labels, value, row.key))
            elif spec.value_pattern is not None:
                result.points.extend(_pattern_points(spec, row, ctx))
            elif _labels_present(spec, row.attrs, skip_pattern_sites=False):
                value = _row_value(spec, row.key, row.attrs, ctx)
                if _numeric(value):
                    labels = _apply_labels(spec, row.key, row.attrs, ctx, skip_pattern_sites=False)
                    result.points.append(ExpectedPoint(labels, value, row.key))
        except (_SkipRow, BuilderError):
            result.error_rows.add(row.key)
    return result


# --------------------------------------------------------------------------- #
# OTLP side
# --------------------------------------------------------------------------- #


@dataclass
class OtelPoint:
    labels: Dict[str, str]
    value: Any
    ts_ns: int


def _attr_value(value: Dict[str, Any]) -> str:
    for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if kind in value:
            raw = value[kind]
            return str(raw).lower() if kind == "boolValue" else str(raw)
    return ""


def _datapoint_value(container: str, dp: Dict[str, Any]) -> Any:
    if container == "histogram":
        return {
            "bucket_counts": [int(c) for c in dp.get("bucketCounts", []) or []],
            "count": int(dp.get("count", 0) or 0),
            "sum": float(dp.get("sum", 0) or 0),
        }
    if "asInt" in dp:
        return int(dp["asInt"])
    if "asDouble" in dp:
        return float(dp["asDouble"])
    return None


def _otel_metrics(docs: Iterable[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    for doc in docs:
        for resource in doc.get("resourceMetrics", []) or []:
            for scope in resource.get("scopeMetrics", []) or []:
                yield from scope.get("metrics", []) or []


def otel_points_by_metric(docs: Iterable[Dict[str, Any]]) -> Dict[str, List[OtelPoint]]:
    """Flatten parsed OTLP JSON into ``{metric: [OtelPoint]}`` (numbers and histograms)."""
    out: Dict[str, List[OtelPoint]] = {}
    for metric in _otel_metrics(docs):
        name = metric.get("name", "")
        for container in ("gauge", "sum", "histogram"):
            for dp in (metric.get(container) or {}).get("dataPoints", []) or []:
                value = _datapoint_value(container, dp)
                if value is None:
                    continue
                labels = {a.get("key", ""): _attr_value(a.get("value") or {}) for a in dp.get("attributes", []) or []}
                out.setdefault(name, []).append(OtelPoint(labels, value, int(dp.get("timeUnixNano", 0) or 0)))
    return out


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

MODE_COUNTER = "counter"
MODE_HISTOGRAM = "histogram-counter"
MODE_GAUGE = "gauge"
MODE_GAUGE_ABS = "gauge-abs"
MODE_EXACT = "exact"
MODE_LABEL = "label"

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_NOT_VALIDATED = "NOT_VALIDATED"


@dataclass
class ComparisonRow:
    metric: str
    family: str
    labels: str
    mode: str
    otel: Any
    pre: Any
    post: Any
    result: str
    detail: str = ""


def metric_family(name: str) -> str:
    for prefix, family in OtelDbValidationConst.FAMILY_BY_PREFIX:
        if name.startswith(prefix):
            return family
    return "other"


def compare_mode(spec: MetricSpec, sample: Any) -> Tuple[str, float]:
    """Pick the check for ``spec`` (see :class:`OtelDbValidationConst` for the tables)."""
    name = spec.name
    if spec.is_histogram:
        return MODE_HISTOGRAM, 0.0
    if spec.metric_type == "sum" or any(s in name for s in OtelDbValidationConst.COUNTER_NAME_SUBSTRINGS):
        return MODE_COUNTER, 0.0
    if any(s in name for s in OtelDbValidationConst.EXACT_NAME_SUBSTRINGS):
        return MODE_EXACT, 0.0
    by_name = [(MODE_GAUGE_ABS, s, limit) for s, limit in OtelDbValidationConst.GAUGE_ABS_TOLERANCE]
    by_name += [(MODE_GAUGE, s, pct) for s, pct in OtelDbValidationConst.GAUGE_DEVIATION_PCT]
    for mode, substring, limit in by_name:
        if substring in name:
            return mode, float(limit)
    if isinstance(sample, float):
        return MODE_GAUGE, float(OtelDbValidationConst.DEFAULT_GAUGE_DEVIATION_PCT)
    return MODE_EXACT, 0.0


def deviation_pct(reference: float, otel: float) -> float:
    """Cumulus ``get_percentage_deviation``, made sign-safe for negative sensor readings."""
    if reference < 0:
        return abs(reference - otel) / abs(reference) * 100
    return OtelDataValidations.get_percentage_deviation(reference, otel)


def _same_number(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    return a == b


def _check_scalar(mode: str, limit: float, otel: Any, pre: List[Any], post: List[Any]) -> Tuple[bool, str]:
    refs = pre + post
    if mode == MODE_COUNTER:
        low = min(pre) if pre else None
        high = max(post) if post else None
        ok = (low is None or otel >= low) and (high is None or otel <= high)
        return ok, f"expected {low} <= otel <= {high}"
    if any(_same_number(otel, ref) for ref in refs):
        return True, "equal"
    within = min(refs) <= otel <= max(refs)
    if mode == MODE_EXACT:
        moving = len({float(r) for r in refs}) > 1
        return moving and within, "value moved between snapshots" if moving else "expected exact match"
    if mode == MODE_GAUGE_ABS:
        best = min(abs(otel - ref) for ref in refs)
        return within or best <= limit, f"abs diff {best:.3f} (max {limit})"
    best_pct = min(deviation_pct(float(ref), float(otel)) for ref in refs)
    return within or best_pct <= limit, f"deviation {best_pct:.2f}% (max {limit}%)"


def _check_histogram(otel: Dict[str, Any], pre: List[Any], post: List[Any]) -> Tuple[bool, str]:
    def column(values: List[Any], index: Optional[int]) -> List[int]:
        return [v["count"] if index is None else v["bucket_counts"][index] for v in values]

    buckets = len(otel["bucket_counts"])
    if any(len(v["bucket_counts"]) != buckets for v in pre + post):
        return False, f"bucket count differs from Redis (otel={buckets})"
    for index in list(range(buckets)) + [None]:
        observed = otel["count"] if index is None else otel["bucket_counts"][index]
        ok, detail = _check_scalar(MODE_COUNTER, 0.0, observed, column(pre, index), column(post, index))
        if not ok:
            where = "count" if index is None else f"bucket {index}"
            return False, f"{where}: otel={observed} {detail}"
    return True, "all buckets within pre/post"


def _label_key(labels: Dict[str, str], names: Iterable[str]) -> LabelKey:
    return tuple(sorted((n, labels[n]) for n in names if n in labels))


def _format_labels(key: LabelKey) -> str:
    return ",".join(f"{k}={v}" for k, v in key)


def _scope_label(spec: MetricSpec) -> str:
    if spec.kind in (KIND_INTERFACE_ID, KIND_INTERFACE_NAME):
        return "interface"
    if spec.kind == KIND_CHANNEL or (spec.kind == KIND_TABLE and spec.table in _TRANSCEIVER_TABLES):
        return "name"
    return ""


@dataclass
class MetricInputs:
    spec: MetricSpec
    pre: ExpectedSet
    post: ExpectedSet
    otel: List[OtelPoint]
    scope_values: Set[str]


def _compare_series(
    spec: MetricSpec, samples: List[OtelPoint], refs: Tuple[List[Any], List[Any]], has_errors: bool,
    window: Tuple[int, int],
) -> Tuple[str, Any, str, str]:
    """``(mode, otel value, result, detail)`` for one exported series; ``refs`` is ``(pre, post)``.

    ``samples`` must be non-empty - callers (:func:`compare_metric`) only reach here
    once they've confirmed the series has at least one OTEL datapoint.
    """
    assert samples, "_compare_series requires at least one OTEL sample"
    pre_vals, post_vals = refs
    latest = max(samples, key=lambda s: s.ts_ns)
    if not pre_vals and not post_vals:
        if has_errors:
            return "-", latest.value, RESULT_NOT_VALIDATED, "Redis row failed to parse"
        return "-", latest.value, RESULT_FAIL, "exported series has no Redis source"
    in_window = [s for s in samples if window[0] <= s.ts_ns <= window[1]]
    if not in_window:
        return "-", latest.value, RESULT_NOT_VALIDATED, "no sample inside the pre/post snapshot window"
    mode, limit = compare_mode(spec, in_window[0].value)
    detail = ""
    for sample in in_window:
        if mode == MODE_HISTOGRAM:
            ok, detail = _check_histogram(sample.value, pre_vals, post_vals)
        else:
            ok, detail = _check_scalar(mode, limit, sample.value, pre_vals, post_vals)
        if not ok:
            return mode, sample.value, RESULT_FAIL, detail
    return mode, in_window[-1].value, RESULT_PASS, detail


def _missing_series_row(spec: MetricSpec, key: LabelKey, pre_vals: List[Any], post_vals: List[Any]) -> ComparisonRow:
    unset = any(_numeric(v) and v == OtelDbValidationConst.UNSET_VALUE for v in pre_vals + post_vals)
    return ComparisonRow(
        metric=spec.name,
        family=metric_family(spec.name),
        labels=_format_labels(key),
        mode="-",
        otel=None,
        pre=pre_vals,
        post=post_vals,
        result=RESULT_NOT_VALIDATED if unset else RESULT_FAIL,
        detail="unset value (-1) not exported" if unset else "Redis has the series but it was not exported",
    )


def _otel_series(inputs: MetricInputs, names: Set[str]) -> Dict[LabelKey, List[OtelPoint]]:
    """Exported series of the metric, restricted to the snapshotted ports / peers / modules."""
    scope_label = _scope_label(inputs.spec)
    allowed = set(inputs.scope_values)
    if scope_label:
        allowed |= {p.labels.get(scope_label, "") for p in inputs.pre.points + inputs.post.points}
    series: Dict[LabelKey, List[OtelPoint]] = {}
    for point in inputs.otel:
        if scope_label and point.labels.get(scope_label) not in allowed:
            continue
        series.setdefault(_label_key(point.labels, names), []).append(point)
    return series


def _index_expected(points: List[ExpectedPoint], names: Iterable[str]) -> Dict[LabelKey, List[ExpectedPoint]]:
    out: Dict[LabelKey, List[ExpectedPoint]] = {}
    for point in points:
        out.setdefault(_label_key(point.labels, names), []).append(point)
    return out


def _label_mismatch_rows(
    spec: MetricSpec,
    identity_key: LabelKey,
    descriptive_names: Iterable[str],
    redis_points: Tuple[Optional[ExpectedPoint], Optional[ExpectedPoint]],
    sample: OtelPoint,
) -> List[ComparisonRow]:
    """One row per declared descriptive label, checked against the pre/post Redis values."""
    rows: List[ComparisonRow] = []
    for name in sorted(descriptive_names):
        expected = {
            point.labels[name] for point in redis_points
            if point is not None and name in point.labels
        }
        if not expected:
            continue
        otel_value = sample.labels.get(name)
        if name not in sample.labels:
            result, detail = RESULT_FAIL, f"label {name} missing"
        elif otel_value not in expected:
            result, detail = RESULT_FAIL, f"label {name}: otel={otel_value} redis={sorted(expected)}"
        else:
            result, detail = RESULT_PASS, "equal"
        rows.append(ComparisonRow(
            metric=spec.name, family=metric_family(spec.name), labels=_format_labels(identity_key),
            mode=MODE_LABEL, otel=otel_value, pre=None, post=None, result=result, detail=detail,
        ))
    return rows


def _compare_matched(
    spec: MetricSpec, key: LabelKey, samples: List[OtelPoint], refs: Tuple[List[Any], List[Any]],
    ctx: Tuple[bool, Tuple[int, int]],
) -> ComparisonRow:
    pre_vals, post_vals = refs
    has_errors, window = ctx
    mode, otel, result, detail = _compare_series(spec, samples, refs, has_errors, window)
    return ComparisonRow(
        metric=spec.name, family=metric_family(spec.name), labels=_format_labels(key), mode=mode,
        otel=otel, pre=pre_vals, post=post_vals, result=result, detail=detail,
    )


def compare_metric(inputs: MetricInputs, *, window: Tuple[int, int]) -> Tuple[List[ComparisonRow], int]:
    """Compare one metric's Redis and OTEL points; returns ``(rows, ignored series count)``.

    Series are paired by :attr:`MetricSpec.identity_labels`; every other declared label is
    then compared value by value against the pre/post Redis reads. An identity with more
    than one distinct Redis row (e.g. repeated transceiver rows per module) falls back to
    matching on the full label set. OTEL-only series and expected series with no Redis
    value on either side are ignored, like an unexpected metric.
    """
    spec = inputs.spec
    all_names: Set[str] = set(spec.labels)
    for point in inputs.pre.points + inputs.post.points:
        all_names |= set(point.labels)
    identity_names = spec.identity_labels & all_names
    descriptive_names = all_names - identity_names

    pre_by_identity = _index_expected(inputs.pre.points, identity_names)
    post_by_identity = _index_expected(inputs.post.points, identity_names)
    otel_by_identity = _otel_series(inputs, identity_names)
    has_errors = bool(inputs.pre.error_rows or inputs.post.error_rows)

    rows: List[ComparisonRow] = []
    ignored = 0
    identities = sorted(set(pre_by_identity) | set(post_by_identity) | set(otel_by_identity))
    for identity_key in identities:
        pre_points = pre_by_identity.get(identity_key, [])
        post_points = post_by_identity.get(identity_key, [])
        samples = otel_by_identity.get(identity_key, [])

        if len(pre_points) > 1 or len(post_points) > 1:
            pre_full = _index_expected(pre_points, all_names)
            post_full = _index_expected(post_points, all_names)
            otel_full: Dict[LabelKey, List[OtelPoint]] = {}
            for sample in samples:
                otel_full.setdefault(_label_key(sample.labels, all_names), []).append(sample)
            for full_key in sorted(set(pre_full) | set(post_full) | set(otel_full)):
                pre_vals = [p.value for p in pre_full.get(full_key, [])]
                post_vals = [p.value for p in post_full.get(full_key, [])]
                full_samples = otel_full.get(full_key, [])
                if not full_samples:
                    if pre_vals or post_vals:
                        rows.append(_missing_series_row(spec, full_key, pre_vals, post_vals))
                    continue
                if not pre_vals and not post_vals:
                    ignored += 1
                    continue
                rows.append(
                    _compare_matched(spec, full_key, full_samples, (pre_vals, post_vals), (has_errors, window))
                )
            continue

        pre_point = pre_points[0] if pre_points else None
        post_point = post_points[0] if post_points else None
        pre_vals = [pre_point.value] if pre_point else []
        post_vals = [post_point.value] if post_point else []

        if not samples:
            if pre_vals or post_vals:
                rows.append(_missing_series_row(spec, identity_key, pre_vals, post_vals))
            else:
                ignored += 1
            continue
        if not pre_vals and not post_vals:
            ignored += 1
            continue

        rows.append(_compare_matched(spec, identity_key, samples, (pre_vals, post_vals), (has_errors, window)))
        in_window = [s for s in samples if window[0] <= s.ts_ns <= window[1]]
        if in_window:
            latest_sample = max(in_window, key=lambda s: s.ts_ns)
            rows.extend(
                _label_mismatch_rows(spec, identity_key, descriptive_names, (pre_point, post_point), latest_sample)
            )

    return rows, ignored


def summarize(rows: List[ComparisonRow], ignored_by_family: Optional[Dict[str, int]] = None) -> str:
    """Per-family counts of compared series, their results, and ignored/not-exported series."""
    ignored_by_family = ignored_by_family or {}
    table: Dict[str, Dict[str, int]] = {}
    for row in rows:
        counts = table.setdefault(row.family, {RESULT_PASS: 0, RESULT_FAIL: 0, RESULT_NOT_VALIDATED: 0})
        counts[row.result] += 1
    families = sorted(set(table) | set(ignored_by_family))
    lines = [f"{'family':<20} {'compared':>9} {'passed':>7} {'failed':>7} {'not validated':>14} {'ignored':>8}"]
    for family in families:
        counts = table.get(family, {RESULT_PASS: 0, RESULT_FAIL: 0, RESULT_NOT_VALIDATED: 0})
        compared = counts[RESULT_PASS] + counts[RESULT_FAIL]
        lines.append(
            f"{family:<20} {compared:>9} {counts[RESULT_PASS]:>7} {counts[RESULT_FAIL]:>7} "
            f"{counts[RESULT_NOT_VALIDATED]:>14} {ignored_by_family.get(family, 0):>8}"
        )
    return "\n".join(lines)


def format_rows(rows: List[ComparisonRow]) -> str:
    """Tab-separated table for the Allure attachment."""
    header = "metric\tlabels\tmode\totel\tdb_pre\tdb_post\tresult\tdetail"
    body = [
        "\t".join(str(v) for v in (r.metric, r.labels, r.mode, r.otel, r.pre, r.post, r.result, r.detail))
        for r in rows
    ]
    return "\n".join([header] + body)
