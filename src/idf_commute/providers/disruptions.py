from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from idf_commute.domain.models import PARIS, Disruption, TimeInterval
from idf_commute.providers.prim_client import PrimClient


class DisruptionSchemaError(ValueError):
    """A bulk disruption response does not match the observed PRIM contract."""


class BulkDisruptionAdapter:
    def __init__(self, client: PrimClient, endpoint_url: str) -> None:
        self._client = client
        self._endpoint_url = endpoint_url

    async def disruptions(
        self,
        interval: TimeInterval | None = None,
    ) -> list[Disruption]:
        response = await self._client.get_json(
            self._endpoint_url,
            cache_key="prim:bulk-disruptions",
            cache_ttl_seconds=120,
        )
        return normalize_bulk_disruptions(response.body, interval=interval)


def normalize_bulk_disruptions(
    payload: Any,
    *,
    interval: TimeInterval | None = None,
) -> list[Disruption]:
    if not isinstance(payload, Mapping):
        raise DisruptionSchemaError("Expected a bulk disruption object")
    raw_disruptions = payload.get("disruptions")
    raw_lines = payload.get("lines")
    if not isinstance(raw_disruptions, list) or not isinstance(raw_lines, list):
        raise DisruptionSchemaError("Expected disruptions and lines arrays")

    affected = _affected_objects_by_disruption(raw_lines)
    normalized: list[Disruption] = []
    for raw in raw_disruptions:
        if not isinstance(raw, Mapping):
            raise DisruptionSchemaError("Every disruption must be an object")
        disruption_id = _required_string(raw.get("id"), "disruption.id")
        periods = tuple(_application_periods(raw.get("applicationPeriods")))
        if (
            interval is not None
            and periods
            and not any(period.intersects(interval) for period in periods)
        ):
            continue
        impact = affected.get(disruption_id, _AffectedObjects())
        normalized.append(
            Disruption(
                id=disruption_id,
                title=_string_or_empty(raw.get("title")),
                message=_string_or_empty(raw.get("message")),
                cause=_optional_string(raw.get("cause")),
                severity=_optional_string(raw.get("severity")),
                effect=_optional_string(raw.get("effect")),
                planned=None,
                application_periods=periods,
                affected_line_ids=frozenset(impact.line_ids),
                affected_stop_area_ids=frozenset(impact.stop_area_ids),
                affected_stop_point_ids=frozenset(impact.stop_point_ids),
                affected_segment=None,
                updated_at=_parse_datetime(raw.get("lastUpdate"), required=False),
                source="prim_disruptions_bulk",
            )
        )
    return normalized


class _AffectedObjects:
    def __init__(self) -> None:
        self.line_ids: set[str] = set()
        self.stop_area_ids: set[str] = set()
        self.stop_point_ids: set[str] = set()


def _affected_objects_by_disruption(
    raw_lines: list[Any],
) -> defaultdict[str, _AffectedObjects]:
    by_disruption: defaultdict[str, _AffectedObjects] = defaultdict(_AffectedObjects)
    for raw_line in raw_lines:
        if not isinstance(raw_line, Mapping):
            continue
        line_id = _optional_string(raw_line.get("id"))
        impacted_objects = raw_line.get("impactedObjects")
        if not isinstance(impacted_objects, list):
            continue
        for raw_object in impacted_objects:
            if not isinstance(raw_object, Mapping):
                continue
            disruption_ids = raw_object.get("disruptionIds")
            if not isinstance(disruption_ids, list):
                continue
            object_id = _optional_string(raw_object.get("id"))
            object_type = _optional_string(raw_object.get("type"))
            for disruption_id in disruption_ids:
                if not isinstance(disruption_id, str):
                    continue
                impact = by_disruption[disruption_id]
                if line_id:
                    impact.line_ids.add(line_id)
                if not object_id:
                    continue
                if object_type == "line" or object_id.startswith("line:"):
                    impact.line_ids.add(object_id)
                elif object_type == "stop_area" or object_id.startswith("stop_area:"):
                    impact.stop_area_ids.add(object_id)
                elif object_type == "stop_point" or object_id.startswith("stop_point:"):
                    impact.stop_point_ids.add(object_id)
    return by_disruption


def _application_periods(value: Any) -> list[TimeInterval]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise DisruptionSchemaError("applicationPeriods must be an array")
    periods: list[TimeInterval] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise DisruptionSchemaError("Every application period must be an object")
        begin = _parse_datetime(raw.get("begin"), required=True)
        end = _parse_datetime(raw.get("end"), required=True)
        if begin is None or end is None:
            raise DisruptionSchemaError("Application period timestamps are required")
        periods.append(TimeInterval(begin=begin, end=end))
    return periods


def _parse_datetime(value: Any, *, required: bool) -> datetime | None:
    if value in (None, ""):
        if required:
            raise DisruptionSchemaError("Required disruption datetime is missing")
        return None
    if not isinstance(value, str):
        raise DisruptionSchemaError("Disruption datetime must be a string")
    try:
        if len(value) == 15 and value[8] == "T":
            return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=PARIS)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DisruptionSchemaError(f"Invalid disruption datetime: {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=PARIS)
    return parsed.astimezone(PARIS)


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise DisruptionSchemaError(f"{field_name} must be a non-empty string")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _string_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""
