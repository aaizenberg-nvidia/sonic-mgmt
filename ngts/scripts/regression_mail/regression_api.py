"""Regression dashboard API adapter."""

from __future__ import annotations

import gzip
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ngts.scripts.regression_mail.models import DashboardAnalysis, DashboardSnapshot
from ngts.scripts.regression_mail.normalization import normalize_text, normalize_version


_COLLECTION_FROM_VERSION = re.compile(r"^(\d{6}_RC\.\d+)")


class RegressionReportClient:
    """Fetch current summary and engineer analysis for one exact version."""

    def __init__(
        self,
        base_url: str,
        timeout: int = 60,
        opener: Any = urlopen,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener

    def fetch(self, version: str) -> DashboardSnapshot:
        normalized_version = normalize_version(version)
        collection_name = self._resolve_collection(normalized_version)
        metric_version = "SONiC." + normalized_version
        collection = self._get_json(
            "/api/collections/{}?rows=1".format(quote(collection_name, safe=""))
        )
        overlay = self._get_json("/api/failure-analysis")
        summary = self._get_json(
            "/api/snapshot-summary",
            {"version": metric_version},
        )
        coverage = self._get_json(
            "/api/coverage",
            {"version": metric_version},
        )
        analyses = self._join_analyses(collection, overlay)
        setup_platforms, setup_testbeds = _setup_maps(
            _require_record_list(collection, "collection")
        )
        return DashboardSnapshot(
            coverage=_extract_coverage(coverage),
            pass_rate=_extract_pass_rate(summary),
            analyses=analyses,
            setup_platforms=setup_platforms,
            setup_testbeds=setup_testbeds,
        )

    def _resolve_collection(self, version: str) -> str:
        match = _COLLECTION_FROM_VERSION.match(normalize_version(version))
        if not match:
            raise ValueError("cannot derive an exact collection name from version {!r}".format(version))
        expected = match.group(1)
        payload = self._get_json("/api/collections")
        names = sorted(_collection_names(payload))
        exact = [name for name in names if name == expected]
        if len(exact) != 1:
            raise ValueError(
                "version {!r} must resolve to exactly one current collection {!r}; found {}".format(
                    version,
                    expected,
                    len(exact),
                )
            )
        return exact[0]

    def _get_json(
        self,
        path: str,
        query: Optional[Mapping[str, str]] = None,
    ) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "sonic-regression-mail/1",
            },
        )
        with self.opener(request, timeout=self.timeout) as response:
            body = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
        return json.loads(body.decode("utf-8"))

    @staticmethod
    def _join_analyses(collection: Any, overlay: Any) -> List[DashboardAnalysis]:
        records = _require_record_list(collection, "collection")
        overlays = _require_record_list(overlay, "failure-analysis")
        overlay_by_key: Dict[str, Mapping[str, Any]] = {}
        for item in overlays:
            key = normalize_text(item.get("recordKey"))
            if key:
                if key in overlay_by_key:
                    raise ValueError(
                        "failure-analysis contains duplicate recordKey {!r}".format(key)
                    )
                overlay_by_key[key] = item

        analyses: List[DashboardAnalysis] = []
        for record in records:
            record_id = normalize_text(record.get("id"))
            if not record_id:
                continue
            record_key = "mars-" + record_id
            engineer = overlay_by_key.get(record_key)
            analysis = (
                str(engineer.get("analysis") or "").strip()
                if engineer
                else ""
            )
            analyses.append(
                DashboardAnalysis(
                    session_id=normalize_text(record.get("sessionId")),
                    key_id=normalize_text(record.get("keyId")),
                    test_name=normalize_text(record.get("name")),
                    analysis=analysis,
                    redmine_url=(
                        normalize_text(engineer.get("rmUrl"))
                        if engineer
                        else ""
                    ),
                )
            )
        return analyses


def _collection_names(payload: Any) -> Iterable[str]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                yield item
            elif isinstance(item, Mapping):
                name = item.get("name") or item.get("collection")
                if name:
                    yield normalize_text(name)
    elif isinstance(payload, Mapping):
        for key in ("collections", "items", "data", "versions"):
            if key in payload:
                yield from _collection_names(payload[key])
                return
        for key in payload:
            if _COLLECTION_FROM_VERSION.match(str(key)):
                yield normalize_text(key)


def _require_record_list(payload: Any, source: str) -> List[Mapping[str, Any]]:
    records = _find_record_list(payload)
    if records is None:
        raise ValueError("{} response has no recognized record list".format(source))
    return records


def _find_record_list(payload: Any) -> Optional[List[Mapping[str, Any]]]:
    if isinstance(payload, list):
        if any(not isinstance(item, Mapping) for item in payload):
            raise ValueError("dashboard record list contains a non-object item")
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("records", "rows", "items", "data", "failures", "results"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                if any(not isinstance(item, Mapping) for item in candidate):
                    raise ValueError("dashboard record list contains a non-object item")
                return [item for item in candidate if isinstance(item, Mapping)]
            if isinstance(candidate, Mapping):
                nested = _find_record_list(candidate)
                if nested is not None:
                    return nested
    return None


def _extract_coverage(payload: Any) -> Optional[float]:
    value = None
    if isinstance(payload, Mapping):
        totals = payload.get("totals")
        if isinstance(totals, Mapping):
            value = totals.get("coverage")
        if value is None:
            nested = payload.get("coverage")
            if isinstance(nested, Mapping):
                nested_totals = nested.get("totals")
                if isinstance(nested_totals, Mapping):
                    value = nested_totals.get("coverage")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_pass_rate(payload: Any) -> Optional[float]:
    groups: Sequence[Any]
    if isinstance(payload, Mapping):
        raw_groups = payload.get("groups") or payload.get("data") or []
        groups = (
            list(raw_groups.values())
            if isinstance(raw_groups, Mapping)
            else raw_groups
        )
    elif isinstance(payload, list):
        groups = payload
    else:
        groups = []
    passed = 0
    failed = 0
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        passed += _as_int(group.get("passed"))
        failed += _as_int(group.get("failed"))
    denominator = passed + failed
    return (passed / denominator * 100.0) if denominator else None


def _setup_maps(
    records: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    platforms_by_setup: Dict[str, List[str]] = {}
    topologies_by_setup: Dict[str, List[str]] = {}
    setups = set()
    for record in records:
        setup = normalize_text(record.get("setupName"))
        if not setup:
            continue
        setups.add(setup)
        platform = normalize_text(record.get("platform"))
        if platform.casefold() == "unknown platform":
            platform = ""
        topology = normalize_text(record.get("topology"))
        if platform:
            platforms_by_setup.setdefault(setup, [])
            if platform not in platforms_by_setup[setup]:
                platforms_by_setup[setup].append(platform)
        if topology:
            topologies_by_setup.setdefault(setup, [])
            if topology not in topologies_by_setup[setup]:
                topologies_by_setup[setup].append(topology)

    setup_platforms: Dict[str, str] = {}
    setup_testbeds: Dict[str, str] = {}
    for setup in sorted(setups):
        platforms = platforms_by_setup.get(setup, [])
        topologies = topologies_by_setup.get(setup, [])
        platform = platforms[0] if len(platforms) == 1 else ""
        topology = topologies[0] if len(topologies) == 1 else ""
        setup_prefix = setup.casefold() + "-"
        if topology.casefold().startswith(setup_prefix):
            topology = topology[len(setup) + 1:]
        setup_platforms[setup] = platform
        setup_testbeds[setup] = (
            "-".join(
                value for value in (platform, topology) if value
            )
            if platform
            else setup
        )
    return setup_platforms, setup_testbeds


def _as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("dashboard count is not an integer: {!r}".format(value)) from error
