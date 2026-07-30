from __future__ import annotations

import json

from idf_commute.redaction import REDACTED, REDACTED_COORDINATE, redact, redact_url


def test_recursive_redaction_removes_token_and_coordinates() -> None:
    secrets = {"super-secret", "48.812345", "2.312345", "2.312345;48.812345"}
    payload = {
        "api_key": "super-secret",
        "nested": {
            "latitude": 48.812345,
            "description": "origin=2.312345;48.812345",
        },
        "items": ["super-secret", "safe"],
    }
    sanitized = redact(payload, secrets)
    serialized = json.dumps(sanitized)

    assert sanitized["api_key"] == REDACTED
    assert sanitized["nested"]["latitude"] == REDACTED_COORDINATE
    assert "super-secret" not in serialized
    assert "48.812345" not in serialized
    assert "2.312345" not in serialized


def test_url_redaction_preserves_non_sensitive_parameters() -> None:
    url = (
        "https://example.test/journeys?"
        "from=2.312345%3B48.812345&data_freshness=realtime&apiKey=secret"
    )
    sanitized = redact_url(url, {"secret", "2.312345;48.812345"})
    assert "secret" not in sanitized
    assert "2.312345" not in sanitized
    assert "data_freshness=realtime" in sanitized


def test_from_and_to_station_ids_are_preserved() -> None:
    payload = {
        "from": {"id": "stop_area:IDFM:123", "coord": {"lat": "48.8", "lon": "2.3"}},
        "to": "stop_area:IDFM:456",
    }
    sanitized = redact(payload, {"48.8", "2.3"})
    assert sanitized["from"]["id"] == "stop_area:IDFM:123"
    assert sanitized["to"] == "stop_area:IDFM:456"
    assert sanitized["from"]["coord"]["lat"] == REDACTED_COORDINATE
