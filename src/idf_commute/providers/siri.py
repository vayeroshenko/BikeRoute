from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from idf_commute.domain.models import PARIS, Departure, Freshness
from idf_commute.providers.prim_client import PrimClient


class SiriSchemaError(ValueError):
    """A SIRI Stop Monitoring response does not match the expected contract."""


class SiriStopMonitoringAdapter:
    def __init__(self, client: PrimClient, endpoint_url: str) -> None:
        self._client = client
        self._endpoint_url = endpoint_url

    async def departures(
        self,
        stop_id: str,
        line_id: str | None = None,
    ) -> list[Departure]:
        response = await self._client.get_json(
            self._endpoint_url,
            params={"MonitoringRef": stop_id},
        )
        departures = normalize_siri_departures(response.body)
        if line_id is None:
            return departures
        return [departure for departure in departures if departure.line_id == line_id]


def normalize_siri_departures(payload: Any) -> list[Departure]:
    if not isinstance(payload, Mapping):
        raise SiriSchemaError("Expected a SIRI response object")
    service_delivery = _mapping(_mapping(payload.get("Siri")).get("ServiceDelivery"))
    if not service_delivery:
        raise SiriSchemaError("Siri.ServiceDelivery is missing")
    response_timestamp = _parse_datetime(
        _value(service_delivery.get("ResponseTimestamp")),
        required=False,
    )
    deliveries = service_delivery.get("StopMonitoringDelivery")
    if not isinstance(deliveries, list):
        raise SiriSchemaError("StopMonitoringDelivery must be an array")

    departures: list[Departure] = []
    for delivery in deliveries:
        visits = _mapping(delivery).get("MonitoredStopVisit")
        if visits is None:
            continue
        if not isinstance(visits, list):
            raise SiriSchemaError("MonitoredStopVisit must be an array")
        for visit in visits:
            journey = _mapping(_mapping(visit).get("MonitoredVehicleJourney"))
            call = _mapping(journey.get("MonitoredCall"))
            stop_id = _value(call.get("StopPointRef"))
            if not isinstance(stop_id, str) or not stop_id:
                raise SiriSchemaError("MonitoredCall.StopPointRef is required")
            expected = _parse_datetime(
                _value(call.get("ExpectedDepartureTime")),
                required=False,
            )
            scheduled = _parse_datetime(
                _value(call.get("AimedDepartureTime")),
                required=False,
            )
            departures.append(
                Departure(
                    line_id=_optional_string(_value(journey.get("LineRef"))),
                    stop_id=stop_id,
                    destination_name=_destination_name(journey.get("DestinationName")),
                    scheduled_time=scheduled,
                    expected_time=expected,
                    response_timestamp=response_timestamp,
                    freshness=(
                        Freshness.REALTIME
                        if expected is not None
                        else Freshness.BASE_SCHEDULE
                        if scheduled is not None
                        else Freshness.UNKNOWN
                    ),
                )
            )
    return departures


def _destination_name(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            name = _value(item)
            if isinstance(name, str):
                return name
        return None
    name = _value(value)
    return name if isinstance(name, str) else None


def _value(value: Any) -> Any:
    if isinstance(value, Mapping) and "value" in value:
        return value["value"]
    return value


def _parse_datetime(value: Any, *, required: bool) -> datetime | None:
    if value in (None, ""):
        if required:
            raise SiriSchemaError("Required SIRI datetime is missing")
        return None
    if not isinstance(value, str):
        raise SiriSchemaError("SIRI datetime must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SiriSchemaError(f"Invalid SIRI datetime: {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=PARIS)
    return parsed.astimezone(PARIS)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
