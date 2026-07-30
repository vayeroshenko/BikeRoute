from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "<redacted>"
REDACTED_COORDINATE = "<redacted-coordinate>"
SENSITIVE_KEY = re.compile(
    r"(api[-_]?key|authorization|access[-_]?token|bearer|password|secret|token)",
    re.IGNORECASE,
)
DIRECT_COORDINATE_KEY = re.compile(
    r"(^|_)(lat|latitude|lon|lng|longitude)($|_)",
    re.IGNORECASE,
)
LOCATION_KEY = re.compile(
    r"(^|_)(from|to|origin|destination|waypoints?)($|_)",
    re.IGNORECASE,
)


def redact_text(value: str, secrets: set[str]) -> str:
    redacted = value
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        redacted = redacted.replace(secret, REDACTED_COORDINATE)
    return redacted


def redact_url(url: str, secrets: set[str]) -> str:
    split = urlsplit(url)
    safe_query: list[tuple[str, str]] = []
    for key, value in parse_qsl(split.query, keep_blank_values=True):
        if SENSITIVE_KEY.search(key):
            safe_value = REDACTED
        elif (
            DIRECT_COORDINATE_KEY.search(key)
            or (LOCATION_KEY.search(key) and _looks_like_coordinate(value))
            or any(secret in value for secret in secrets)
        ):
            safe_value = REDACTED_COORDINATE
        else:
            safe_value = redact_text(value, secrets)
        safe_query.append((key, safe_value))
    return urlunsplit(
        (split.scheme, split.netloc, split.path, urlencode(safe_query), split.fragment)
    )


def redact(value: Any, secrets: set[str], *, key: str | None = None) -> Any:
    if key and SENSITIVE_KEY.search(key):
        return REDACTED
    if key and DIRECT_COORDINATE_KEY.search(key):
        return REDACTED_COORDINATE
    if isinstance(value, str):
        if key and LOCATION_KEY.search(key) and _looks_like_coordinate(value):
            return REDACTED_COORDINATE
        if value.startswith(("http://", "https://")):
            return redact_url(value, secrets)
        return redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            str(item_key): redact(item, secrets, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, (int, float)) and str(value) in secrets:
        return REDACTED_COORDINATE
    return value


def redact_mapping(value: Mapping[str, Any], secrets: set[str]) -> dict[str, Any]:
    return {str(key): redact(item, secrets, key=str(key)) for key, item in value.items()}


def _looks_like_coordinate(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*-?\d{1,3}(?:\.\d+)?\s*[;,]\s*-?\d{1,3}(?:\.\d+)?\s*",
            value,
        )
    )
