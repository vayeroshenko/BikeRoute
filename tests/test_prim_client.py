from __future__ import annotations

from typing import Any

import httpx
import pytest

from idf_commute.providers.prim_client import (
    PrimClient,
    PrimEdgeAccessError,
    PrimRateLimitError,
)


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    async def get(self, key: str) -> Any | None:
        return self.values.get(key)

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        assert ttl_seconds > 0
        self.values[key] = value


@pytest.mark.asyncio
async def test_retries_429_honors_retry_after_and_records_metadata() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.headers["apiKey"] == "secret"
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(
            200,
            json={"lastUpdatedDate": "2026-07-30T09:02:11.434Z"},
            headers={"X-RateLimit-Remaining": "998"},
        )

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with PrimClient(
        "secret",
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    ) as client:
        response = await client.get_json("https://api.test/disruptions")

    assert attempts == 2
    assert sleeps == [2]
    assert response.metadata.response_timestamp == "2026-07-30T09:02:11.434Z"
    assert response.metadata.quota_headers["x-ratelimit-remaining"] == "998"


@pytest.mark.asyncio
async def test_repeated_429_is_bounded() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    async def sleep(_: float) -> None:
        return None

    async with PrimClient(
        "secret",
        max_attempts=2,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    ) as client:
        with pytest.raises(PrimRateLimitError, match="after 2 attempts"):
            await client.get_json("https://api.test/limited")


@pytest.mark.asyncio
async def test_html_403_is_distinguished_from_subscription_error() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html>blocked</html>")

    async with PrimClient("secret", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PrimEdgeAccessError, match="edge/security"):
            await client.get_json("https://api.test/blocked")


@pytest.mark.asyncio
async def test_cache_hook_avoids_second_request() -> None:
    requests = 0
    cache = MemoryCache()

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"value": 1})

    async with PrimClient(
        "secret",
        cache=cache,
        transport=httpx.MockTransport(handler),
    ) as client:
        first = await client.get_json(
            "https://api.test/static",
            cache_key="static",
            cache_ttl_seconds=60,
        )
        second = await client.get_json(
            "https://api.test/static",
            cache_key="static",
            cache_ttl_seconds=60,
        )

    assert first.metadata.from_cache is False
    assert second.metadata.from_cache is True
    assert requests == 1
