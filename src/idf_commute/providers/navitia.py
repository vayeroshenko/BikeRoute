from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from idf_commute.domain.models import (
    PARIS,
    Freshness,
    TransitJourney,
    TransitLeg,
    TransitRequest,
)
from idf_commute.providers.prim_client import PrimClient


class NavitiaSchemaError(ValueError):
    """A Navitia response does not match the observed journey contract."""


class NavitiaAdapter:
    def __init__(self, client: PrimClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def journeys(self, request: TransitRequest) -> list[TransitJourney]:
        params: dict[str, Any] = {
            "from": request.origin_id,
            "to": request.destination_id,
            "data_freshness": request.data_freshness.value,
            "direct_path": "none",
        }
        if request.datetime is not None:
            params["datetime"] = request.datetime.astimezone(PARIS).strftime("%Y%m%dT%H%M%S")
            params["datetime_represents"] = "arrival" if request.arrive_by else "departure"
        if request.forbidden_ids:
            params["forbidden_uris[]"] = list(request.forbidden_ids)
        response = await self._client.get_json(f"{self._base_url}/journeys", params=params)
        return normalize_navitia_journeys(response.body)


def normalize_navitia_journeys(payload: Any) -> list[TransitJourney]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("journeys"), list):
        raise NavitiaSchemaError("Expected an object containing a journeys array")
    response_timestamp = _parse_navitia_datetime(
        _mapping(payload.get("context")).get("current_datetime"),
        required=False,
    )
    journeys: list[TransitJourney] = []
    for raw in payload["journeys"]:
        if not isinstance(raw, Mapping):
            raise NavitiaSchemaError("Every journey must be an object")
        journeys.append(_normalize_journey(raw, response_timestamp))
    return journeys


def _normalize_journey(
    raw: Mapping[str, Any],
    response_timestamp: datetime | None,
) -> TransitJourney:
    sections = raw.get("sections")
    if not isinstance(sections, list):
        raise NavitiaSchemaError("journey.sections must be an array")
    legs = tuple(_normalize_leg(section) for section in sections if isinstance(section, Mapping))
    if len(legs) != len(sections):
        raise NavitiaSchemaError("Every journey section must be an object")
    return TransitJourney(
        id=_optional_string(raw.get("id")),
        type=_optional_string(raw.get("type")),
        status=_optional_string(raw.get("status")),
        duration_seconds=_integer(raw.get("duration"), "journey.duration"),
        departure=_required_datetime(raw.get("departure_date_time"), "journey.departure"),
        arrival=_required_datetime(raw.get("arrival_date_time"), "journey.arrival"),
        requested_datetime=_parse_navitia_datetime(
            raw.get("requested_date_time"),
            required=False,
        ),
        transfers=_optional_integer(raw.get("nb_transfers"), default=0),
        legs=legs,
        response_timestamp=response_timestamp,
    )


def _normalize_leg(raw: Mapping[str, Any]) -> TransitLeg:
    freshness_value = _optional_string(raw.get("data_freshness"))
    try:
        freshness = Freshness(freshness_value) if freshness_value else Freshness.UNKNOWN
    except ValueError:
        freshness = Freshness.UNKNOWN
    display = _mapping(raw.get("display_informations"))
    return TransitLeg(
        type=_required_string(raw.get("type"), "section.type"),
        mode=_optional_string(raw.get("mode")),
        duration_seconds=_optional_integer(raw.get("duration"), default=0),
        departure=_parse_navitia_datetime(raw.get("departure_date_time"), required=False),
        arrival=_parse_navitia_datetime(raw.get("arrival_date_time"), required=False),
        base_departure=_parse_navitia_datetime(
            raw.get("base_departure_date_time"),
            required=False,
        ),
        base_arrival=_parse_navitia_datetime(
            raw.get("base_arrival_date_time"),
            required=False,
        ),
        freshness=freshness,
        line_id=_linked_id(raw.get("links"), "line"),
        line_code=_optional_string(display.get("code") or display.get("label")),
        origin_id=_place_id(raw.get("from")),
        destination_id=_place_id(raw.get("to")),
        disruption_ids=tuple(_linked_ids(raw.get("links"), "disruption")),
    )


def _parse_navitia_datetime(value: Any, *, required: bool) -> datetime | None:
    if value in (None, ""):
        if required:
            raise NavitiaSchemaError("Required Navitia datetime is missing")
        return None
    if not isinstance(value, str):
        raise NavitiaSchemaError("Navitia datetime must be a string")
    try:
        if len(value) == 15 and value[8] == "T":
            return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=PARIS)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NavitiaSchemaError(f"Invalid Navitia datetime: {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=PARIS)
    return parsed.astimezone(PARIS)


def _required_datetime(value: Any, field_name: str) -> datetime:
    parsed = _parse_navitia_datetime(value, required=True)
    if parsed is None:
        raise NavitiaSchemaError(f"{field_name} is required")
    return parsed


def _linked_id(value: Any, object_type: str) -> str | None:
    ids = _linked_ids(value, object_type)
    return ids[0] if ids else None


def _linked_ids(value: Any, object_type: str) -> list[str]:
    if not isinstance(value, list):
        return []
    identifiers: list[str] = []
    for link in value:
        if not isinstance(link, Mapping):
            continue
        if link.get("type") not in {object_type, f"{object_type}s"}:
            continue
        identifier = link.get("id")
        if isinstance(identifier, str):
            identifiers.append(identifier)
    return identifiers


def _place_id(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    identifier = value.get("id")
    if isinstance(identifier, str):
        return identifier
    for nested_key in ("stop_area", "stop_point", "address"):
        nested = value.get(nested_key)
        if isinstance(nested, Mapping) and isinstance(nested.get("id"), str):
            nested_id = nested.get("id")
            return nested_id if isinstance(nested_id, str) else None
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise NavitiaSchemaError(f"{field_name} must be a non-empty string")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NavitiaSchemaError(f"{field_name} must be numeric")
    return int(value)


def _optional_integer(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    return int(value) if isinstance(value, (int, float)) else default
