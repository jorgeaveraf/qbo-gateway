from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Optional

import httpx
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt, wait_none

from app.core.config import Settings, get_settings


class RetryableHTTPException(Exception):
    def __init__(self, response: httpx.Response, retry_after: Optional[float] = None):
        self.response = response
        self.retry_after = retry_after
        super().__init__(f"Retryable HTTP error: {response.status_code}")


def _parse_retry_after(response: httpx.Response) -> Optional[float]:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        value = float(header)
        return max(value, 0.0)
    except ValueError:
        try:
            retry_time = datetime.strptime(header, "%a, %d %b %Y %H:%M:%S %Z")
            delta = (retry_time - datetime.utcnow()).total_seconds()
            return max(delta, 0.0)
        except ValueError:
            return None


def _calculate_wait(settings: Settings, retry_state: RetryCallState) -> float:
    if retry_state.outcome is not None and retry_state.outcome.failed:
        exc = retry_state.outcome.exception()
        if isinstance(exc, RetryableHTTPException) and exc.retry_after is not None:
            return min(exc.retry_after, settings.retry_max_wait_seconds)
    attempt = retry_state.attempt_number
    backoff = min(settings.retry_max_wait_seconds, 2 ** (attempt - 1))
    return backoff


def _should_retry(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if 500 <= response.status_code < 600:
        return True
    return False


def get_async_client(settings: Settings | None = None) -> httpx.AsyncClient:
    settings = settings or get_settings()
    timeout = httpx.Timeout(settings.http_timeout_seconds)
    return httpx.AsyncClient(timeout=timeout)


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    settings: Settings | None = None,
    **kwargs: Any,
) -> httpx.Response:
    settings = settings or get_settings()

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(settings.retry_max_attempts),
        retry=retry_if_exception_type(RetryableHTTPException),
        wait=wait_none(),
        reraise=True,
    ):
        with attempt:
            response = await client.request(method, url, **kwargs)
            if _should_retry(response):
                retry_after = _parse_retry_after(response)
                raise RetryableHTTPException(response, retry_after)
            return response


async def request_with_retry_and_backoff(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    settings: Settings | None = None,
    **kwargs: Any,
) -> httpx.Response:
    settings = settings or get_settings()

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(settings.retry_max_attempts),
        retry=retry_if_exception_type(RetryableHTTPException),
        wait=lambda retry_state: _calculate_wait(settings, retry_state),
        reraise=True,
    ):
        with attempt:
            response = await client.request(method, url, **kwargs)
            if _should_retry(response):
                retry_after = _parse_retry_after(response)
                raise RetryableHTTPException(response, retry_after)
            return response
