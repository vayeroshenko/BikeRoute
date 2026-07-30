from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol

import httpx
from pydantic import SecretStr


class PrimError(RuntimeError):
    """Base error for PRIM transport and response failures."""


class PrimAuthenticationError(PrimError):
    """The PRIM token is absent, expired, or invalid."""


class PrimSubscriptionError(PrimError):
    """The token does not have access to a requested API."""


class PrimEdgeAccessError(PrimError):
    """A non-API edge/security layer rejected the request."""


class PrimRateLimitError(PrimError):
    """PRIM continued to return 429 after bounded retries."""


class PrimRequestBudgetError(PrimError):
    """A planning run exhausted its configured PRIM request budget."""


class PrimResponseError(PrimError):
    """PRIM returned an unexpected status or malformed body."""


class JsonCache(Protocol):
    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None: ...


@dataclass(frozen=True)
class ResponseMetadata:
    status_code: int
    received_at: datetime
    latency_ms: float
    response_timestamp: str | None
    quota_headers: Mapping[str, str]
    from_cache: bool = False


@dataclass(frozen=True)
class JsonResponse:
    body: Any
    metadata: ResponseMetadata


Sleep = Callable[[float], Awaitable[None]]


class PrimClient:
    def __init__(
        self,
        api_key: SecretStr | str,
        *,
        api_key_header: str = "apiKey",
        timeout_seconds: float = 20.0,
        max_attempts: int = 3,
        max_requests: int | None = None,
        cache: JsonCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        token = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not token:
            raise PrimAuthenticationError("A non-empty PRIM API key is required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if max_requests is not None and max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        self._max_attempts = max_attempts
        self._max_requests = max_requests
        self._request_count = 0
        self._cache = cache
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            headers={api_key_header: token, "Accept": "application/json"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> PrimClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def max_requests(self) -> int | None:
        return self._max_requests

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        cache_key: str | None = None,
        cache_ttl_seconds: int = 0,
    ) -> JsonResponse:
        return await self.request_json(
            "GET",
            url,
            params=params,
            cache_key=cache_key,
            cache_ttl_seconds=cache_ttl_seconds,
        )

    async def post_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        cache_key: str | None = None,
        cache_ttl_seconds: int = 0,
    ) -> JsonResponse:
        return await self.request_json(
            "POST",
            url,
            params=params,
            json_body=json_body,
            cache_key=cache_key,
            cache_ttl_seconds=cache_ttl_seconds,
        )

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        cache_key: str | None = None,
        cache_ttl_seconds: int = 0,
    ) -> JsonResponse:
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return JsonResponse(
                body=cached,
                metadata=ResponseMetadata(
                    status_code=200,
                    received_at=datetime.now(UTC),
                    latency_ms=0,
                    response_timestamp=_response_timestamp(cached),
                    quota_headers={},
                    from_cache=True,
                ),
            )

        last_response: httpx.Response | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._claim_request()
            started = monotonic()
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self._max_attempts:
                    raise PrimResponseError(
                        f"PRIM request failed after {attempt} attempts: {type(exc).__name__}"
                    ) from exc
                await self._sleep(_backoff_seconds(attempt, None))
                continue

            last_response = response
            latency_ms = (monotonic() - started) * 1000
            if response.status_code == 401:
                raise PrimAuthenticationError("PRIM returned HTTP 401")
            if response.status_code == 403:
                if "json" in response.headers.get("content-type", "").casefold():
                    raise PrimSubscriptionError("PRIM returned JSON HTTP 403")
                raise PrimEdgeAccessError("PRIM edge/security layer returned HTTP 403")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self._max_attempts:
                    await self._sleep(
                        _backoff_seconds(attempt, response.headers.get("retry-after"))
                    )
                    continue
                if response.status_code == 429:
                    raise PrimRateLimitError(f"PRIM returned HTTP 429 after {attempt} attempts")
                raise PrimResponseError(
                    f"PRIM returned HTTP {response.status_code} after {attempt} attempts"
                )
            if response.status_code >= 400:
                raise PrimResponseError(f"PRIM returned HTTP {response.status_code}")

            try:
                body = response.json()
            except ValueError as exc:
                raise PrimResponseError("PRIM returned a non-JSON success response") from exc
            await self._set_cached(cache_key, body, cache_ttl_seconds)
            return JsonResponse(
                body=body,
                metadata=ResponseMetadata(
                    status_code=response.status_code,
                    received_at=datetime.now(UTC),
                    latency_ms=latency_ms,
                    response_timestamp=_response_timestamp(body),
                    quota_headers=_quota_headers(response.headers),
                ),
            )

        status = last_response.status_code if last_response is not None else "unknown"
        raise PrimResponseError(f"PRIM request failed with final status {status}")

    def _claim_request(self) -> None:
        if self._max_requests is not None and self._request_count >= self._max_requests:
            raise PrimRequestBudgetError(
                f"PRIM request budget of {self._max_requests} exhausted "
                "before the next provider call"
            )
        self._request_count += 1

    async def _get_cached(self, cache_key: str | None) -> Any | None:
        if self._cache is None or cache_key is None:
            return None
        return await self._cache.get(cache_key)

    async def _set_cached(self, cache_key: str | None, body: Any, ttl_seconds: int) -> None:
        if self._cache is None or cache_key is None or ttl_seconds <= 0:
            return
        await self._cache.set(cache_key, body, ttl_seconds)


def _backoff_seconds(attempt: int, retry_after: str | None) -> float:
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0), 30)
        except ValueError:
            pass
    return float(min(0.25 * (2 ** (attempt - 1)) + random.uniform(0, 0.1), 5))


def _quota_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower().startswith(("x-ratelimit-", "ratelimit-"))
    }


def _response_timestamp(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("lastUpdatedDate", "response_timestamp", "ResponseTimestamp"):
        value = body.get(key)
        if isinstance(value, str):
            return value
    context = body.get("context")
    if isinstance(context, dict):
        current_datetime = context.get("current_datetime")
        if isinstance(current_datetime, str):
            return current_datetime
    return None
