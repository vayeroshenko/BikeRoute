from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from idf_commute.domain.models import Location, TimeInterval

PARIS = ZoneInfo("Europe/Paris")


def test_time_interval_requires_aware_ordered_datetimes() -> None:
    begin = datetime(2026, 7, 30, 8, 0, tzinfo=PARIS)
    interval = TimeInterval(begin=begin, end=begin + timedelta(hours=1))
    assert interval.intersects(
        TimeInterval(
            begin=begin + timedelta(minutes=30),
            end=begin + timedelta(hours=2),
        )
    )
    with pytest.raises(ValidationError, match="timezone-aware"):
        TimeInterval(
            begin=datetime(2026, 7, 30, 8, 0),
            end=datetime(2026, 7, 30, 9, 0),
        )


def test_location_is_limited_to_broad_idf_bounds() -> None:
    assert Location(latitude=48.8, longitude=2.3).latitude == 48.8
    with pytest.raises(ValidationError):
        Location(latitude=45.0, longitude=2.3)
