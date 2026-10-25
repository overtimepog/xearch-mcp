"""HTTP helpers with retries, timeouts, and a tiny rate limiter."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx2

from .config import Settings, get_settings
from .errors import UpstreamError

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS = (
    httpx2.TimeoutException,
    httpx2.ConnectError,
    httpx2.ReadError,
    httpx2.ProxyError,
    httpx2.RemoteProtocolError,
)


class AsyncRateLimiter:
    """Serialize calls to a host with a minimum interval between them."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval = max(0.0, min_interval_seconds)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class HttpClient:
    """A shared async HTTP client."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(self.settings.timeout_seconds),
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            },
            trust_env=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 2,
        follow_redirects: bool | None = None,
    ) -> httpx2.Response:
        """Send a request, retrying transient failures.

        Raises:
            UpstreamError: for a non-2xx response or a transport failure.
        """
        last_error: Exception | None = None
        attempts = max(0, retries) + 1
        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json,
                    headers=headers,
                    follow_redirects=follow_redirects,
                )
            except RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await self._backoff(attempt)
                    continue
                raise UpstreamError(
                    f"Request failed: {exc.__class__.__name__}: {exc}", url=url
                ) from exc
            except httpx2.HTTPError as exc:
                raise UpstreamError(
                    f"Request failed: {exc.__class__.__name__}: {exc}", url=url
                ) from exc

            if response.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                last_error = UpstreamError(
                    f"HTTP {response.status_code} from {url}",
                    status=response.status_code,
                    url=url,
                )
                await self._backoff(attempt)
                continue

            if response.status_code >= 400:
                raise UpstreamError(
                    f"HTTP {response.status_code} from {url}",
                    status=response.status_code,
                    url=url,
                )
            return response

        # Defensive: the loop above should always return or raise.
        raise UpstreamError(f"Request failed: {last_error}", url=url)

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 2,
        follow_redirects: bool | None = None,
    ) -> httpx2.Response:
        return await self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            retries=retries,
            follow_redirects=follow_redirects,
        )

    async def post_form(
        self,
        url: str,
        *,
        data: dict[str, Any],
        headers: dict[str, str] | None = None,
        retries: int = 1,
        follow_redirects: bool | None = None,
    ) -> httpx2.Response:
        return await self.request(
            "POST",
            url,
            data=data,
            headers=headers,
            retries=retries,
            follow_redirects=follow_redirects,
        )

    async def post_json(
        self,
        url: str,
        *,
        json: Any,
        headers: dict[str, str] | None = None,
        retries: int = 1,
    ) -> httpx2.Response:
        return await self.request(
            "POST",
            url,
            json=json,
            headers=headers,
            retries=retries,
        )

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 2,
    ) -> Any:
        response = await self.get(url, params=params, headers=headers, retries=retries)
        try:
            return response.json()
        except ValueError as exc:
            preview = response.text[:200].replace("\n", " ")
            raise UpstreamError(f"Expected JSON from {url}, got: {preview!r}", url=url) from exc

    async def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 2,
    ) -> str:
        response = await self.get(url, params=params, headers=headers, retries=retries)
        return response.text

    @staticmethod
    async def _backoff(attempt: int) -> None:
        delay = min(4.0, 0.5 * (2**attempt)) + random.uniform(0.0, 0.35)
        await asyncio.sleep(delay)
