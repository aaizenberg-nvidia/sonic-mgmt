"""Regression dashboard API adapter."""

from __future__ import annotations

import gzip
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ngts.scripts.regression_mail.models import DashboardAnalysis, DashboardSnapshot
from ngts.scripts.regression_mail.normalization import normalize_text, normalize_version


_COLLECTION_VERSION = re.compile(r"^(\d{6}_RC\.\d+)")


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

    def fetch(self, versions: Union[str, Sequence[str]]) -> DashboardSnapshot:
        normalized_versions = _normalized_versions(versions)
        overlay_records: List[Mapping[str, Any]] = []
        seen_overlay = set()
        for version in normalized_versions:
            payload = self._get_json(
                "/api/failure-analysis",
                {"version": "SONiC." + version},
            )
            for record in _require_record_list(payload, "failure-analysis"):
                identity = (
                    normalize_text(record.get("recordKey")),
                    normalize_text(record.get("updatedAt")),
                    normalize_text(record.get("marsSessionId")),
                    normalize_text(record.get("testName")),
                )
                if identity not in seen_overlay:
                    seen_overlay.add(identity)
                    overlay_records.append(record)
        overlay = {"records": overlay_records}
        wanted = set(normalized_versions)
        direct_records = _latest_records_by_key([
            record
            for record in overlay_records
            if normalize_version(record.get("switchVersion")) in wanted and
            normalize_text(record.get("marsSessionId")) and
            normalize_text(record.get("testName"))
        ])
        direct_analyses = self._analyses_from_records(direct_records)
        try:
            collection_records = self._collection_records(normalized_versions)
        except Exception:
            if not direct_analyses:
                raise
            collection_records = []
        hydrated = self._join_analyses(collection_records, overlay)
        analyses = _merge_analyses(hydrated, direct_analyses)
        setup_platforms, setup_testbeds = _setup_maps(collection_records)
        return DashboardSnapshot(
            analyses=analyses,
            setup_platforms=setup_platforms,
            setup_testbeds=setup_testbeds,
        )

    def _collection_records(
        self,
        versions: Sequence[str],
    ) -> List[Mapping[str, Any]]:
        wanted = set(versions)
        collections = _require_record_list(
            self._get_json("/api/collections"),
            "collections",
        )
        records_by_id: Dict[Tuple[str, str], Mapping[str, Any]] = {}
        for collection in collections:
            collection_versions = {
                normalize_version(version)
                for version in _collection_versions(collection)
            }
            if not wanted.intersection(collection_versions):
                continue
            name = normalize_text(collection.get("name"))
            if not name:
                continue
            payload = self._get_json(
                "/api/collections/{}".format(quote(name, safe="")),
                {"rows": "1"},
            )
            for record in _require_record_list(payload, "collection"):
                if normalize_version(record.get("version")) not in wanted:
                    continue
                record_id = normalize_text(record.get("id"))
                if record_id:
                    identity = (
                        normalize_version(record.get("version")),
                        record_id,
                    )
                    current = records_by_id.get(identity)
                    if current is not None and current != record:
                        raise ValueError(
                            "dashboard collections contain conflicting record {!r} "
                            "for version {!r}".format(record_id, identity[0])
                        )
                    records_by_id[identity] = record
        return list(records_by_id.values())

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
    def _analyses_from_records(records: Sequence[Mapping[str, Any]]) -> List[DashboardAnalysis]:
        analyses: List[DashboardAnalysis] = []
        for record in records:
            analyses.append(
                DashboardAnalysis(
                    session_id=normalize_text(record.get("marsSessionId")),
                    key_id=_dashboard_key_id(record),
                    test_name=normalize_text(record.get("testName")),
                    analysis=normalize_text(record.get("analysis")),
                    redmine_url=normalize_text(record.get("rmUrl")),
                    verdict=normalize_text(record.get("verdict")),
                    updated_at=normalize_text(record.get("updatedAt")),
                    switch_version=normalize_version(record.get("switchVersion")),
                )
            )
        return analyses

    @staticmethod
    def _join_analyses(collection: Any, overlay: Any) -> List[DashboardAnalysis]:
        records = _require_record_list(collection, "collection")
        overlays = _require_record_list(overlay, "failure-analysis")
        overlay_by_key: Dict[str, Mapping[str, Any]] = {}
        for item in overlays:
            key = normalize_text(item.get("recordKey"))
            if key:
                current = overlay_by_key.get(key)
                if current is None or normalize_text(item.get("updatedAt")) > normalize_text(
                    current.get("updatedAt")
                ):
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
                    verdict=normalize_text(record.get("verdict")),
                    updated_at=(
                        normalize_text(engineer.get("updatedAt"))
                        if engineer
                        else ""
                    ),
                    switch_version=normalize_version(record.get("version")),
                )
            )
        return analyses


def _normalized_versions(versions: Union[str, Sequence[str]]) -> List[str]:
    values = [versions] if isinstance(versions, str) else list(versions)
    result = []
    for value in values:
        normalized = normalize_version(value)
        if normalized and normalized not in result:
            result.append(normalized)
    if not result:
        raise ValueError("at least one SONiC version is required")
    return result


def _collection_versions(collection: Mapping[str, Any]) -> Iterable[str]:
    filters = collection.get("filters")
    if isinstance(filters, Mapping):
        versions = filters.get("versions")
        if isinstance(versions, list):
            yield from (normalize_text(version) for version in versions)
            return
    name = normalize_text(collection.get("name"))
    match = _COLLECTION_VERSION.match(name)
    if match:
        yield match.group(1)


def _require_record_list(payload: Any, source: str) -> List[Mapping[str, Any]]:
    records = _find_record_list(payload)
    if records is None:
        raise ValueError("{} response has no recognized record list".format(source))
    return records


def _latest_records_by_key(
    records: Sequence[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    """Keep the latest dashboard state for each stable failure record."""

    selected: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    unkeyed: List[Mapping[str, Any]] = []
    for record in records:
        key = normalize_text(record.get("recordKey"))
        if not key:
            unkeyed.append(record)
            continue
        identity = (
            normalize_text(record.get("marsSessionId")),
            key,
        )
        current = selected.get(identity)
        if current is None or normalize_text(record.get("updatedAt")) > normalize_text(
            current.get("updatedAt")
        ):
            selected[identity] = record
    return unkeyed + list(selected.values())


def _dashboard_key_id(record: Mapping[str, Any]) -> str:
    return normalize_text(record.get("keyId"))


def _analysis_identity(analysis: DashboardAnalysis) -> Tuple[str, str, str, str]:
    return (
        normalize_version(analysis.switch_version),
        normalize_text(analysis.session_id),
        normalize_text(analysis.key_id),
        normalize_text(analysis.test_name),
    )


def _merge_analyses(
    primary: Sequence[DashboardAnalysis],
    fallback: Sequence[DashboardAnalysis],
) -> List[DashboardAnalysis]:
    result = list(primary)
    identities = {_analysis_identity(analysis) for analysis in result}
    for analysis in fallback:
        identity = _analysis_identity(analysis)
        if identity not in identities:
            identities.add(identity)
            result.append(analysis)
    return result


def _find_record_list(payload: Any) -> Optional[List[Mapping[str, Any]]]:
    if isinstance(payload, list):
        if any(not isinstance(item, Mapping) for item in payload):
            raise ValueError("dashboard record list contains a non-object item")
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in (
            "records", "rows", "items", "data", "failures", "results",
            "collections",
        ):
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
