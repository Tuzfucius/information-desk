"""Bounded HTTPS client for the supported source adapters."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Self
from urllib.parse import urljoin, urlsplit

import httpx

from info_desk.exceptions import InfoDeskError, UpstreamError

ALLOWED_HOSTS = {
    "arxiv.org",
    "export.arxiv.org",
    "github.com",
    "api.github.com",
    "huggingface.co",
}


class SafeHttpClient:
    def __init__(
        self,
        *,
        max_response_bytes: int = 4_000_000,
        max_retries: int = 3,
        github_token: str | None = None,
        hf_token: str | None = None,
        timeout: httpx.Timeout | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.max_response_bytes = max_response_bytes
        self.max_retries = max_retries
        self.clock = clock
        self.sleep = sleep
        self._allowed_hosts = set(ALLOWED_HOSTS)
        self._source_intervals: dict[str, float] = {}
        self._last_request: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self.client = httpx.AsyncClient(
            timeout=timeout or httpx.Timeout(connect=5, read=20, write=10, pool=5),
            follow_redirects=False,
            headers={"User-Agent": "information-desk/0.1"},
            transport=transport,
        )
        self.tokens = {"api.github.com": github_token, "huggingface.co": hf_token}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs: object):
        self._validate_url(url)
        host = urlsplit(url).hostname or ""
        headers = dict(kwargs.pop("headers", {}) or {})
        token = self.tokens.get(host)
        if token:
            headers.setdefault("Authorization", f"Bearer {token}")
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        interval = self._request_interval(host, url)
        async with lock:
            delay = interval - (self.clock() - self._last_request.get(host, 0))
            if delay > 0:
                await self.sleep(delay)
            self._last_request[host] = self.clock()
            async with self.client.stream(method, url, headers=headers, **kwargs) as response:
                # Keep the host lock through body consumption, so concurrent callers cannot
                # bypass the per-host request limit while a response is still streaming.
                yield response

    def _request_interval(self, host: str, url: str) -> float:
        path = urlsplit(url).path
        if host == "export.arxiv.org":
            baseline = 3.0
        elif host == "api.github.com" and path.startswith("/search/"):
            baseline = 2.1 if self.tokens.get(host) else 6.1
        else:
            baseline = 0.1
        return max(baseline, self._source_intervals.get(host, 0.0))

    def register_source_policy(
        self, *, request_hosts: tuple[str, ...], min_interval_seconds: float
    ) -> None:
        """Explicitly allow and rate-bound hosts declared by a registered adapter."""
        if min_interval_seconds < 0 or not math.isfinite(min_interval_seconds):
            raise ValueError("minimum request interval must be finite and nonnegative")
        for host in request_hosts:
            normalized = host.lower()
            labels = host.split(".")
            if (
                normalized != host
                or not host.isascii()
                or any(
                    not label
                    or len(label) > 63
                    or not label[0].isalnum()
                    or not label[-1].isalnum()
                    or any(not (char.isalnum() or char == "-") for char in label)
                    for label in labels
                )
                or len(host) > 253
            ):
                raise ValueError("request host must be a lowercase ASCII DNS name")
            self._allowed_hosts.add(host)
            self._source_intervals[host] = max(
                self._source_intervals.get(host, 0.0), min_interval_seconds
            )

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        """GET through the same rate, retry, redirect, and body-size policy as adapters."""
        from info_desk.sources.base import bounded_get

        current_url = url
        original_host = urlsplit(url).hostname
        request_kwargs = dict(kwargs)
        for redirect_count in range(4):
            try:
                response = await bounded_get(
                    self,
                    current_url,
                    max_bytes=self.max_response_bytes,
                    **request_kwargs,
                )
            except httpx.TimeoutException as error:
                raise UpstreamError("UPSTREAM_TIMEOUT", "The source request timed out.") from error
            except httpx.HTTPError as error:
                raise UpstreamError(
                    "UPSTREAM_UNAVAILABLE", "The source request failed."
                ) from error
            if not response.is_redirect:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            if redirect_count == 3:
                break
            next_url = urljoin(str(response.request.url), location)
            self._validate_url(next_url)
            next_host = urlsplit(next_url).hostname
            headers = dict(request_kwargs.get("headers", {}) or {})
            if next_host != original_host:
                headers = {
                    key: value
                    for key, value in headers.items()
                    if key.lower() not in {"authorization", "cookie"}
                }
            current_url = next_url
            request_kwargs = {
                key: value for key, value in request_kwargs.items() if key != "params"
            }
            if headers:
                request_kwargs["headers"] = headers
            else:
                request_kwargs.pop("headers", None)
        raise InfoDeskError("INVALID_TARGET", "The source redirected too many times.")

    def _validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as error:
            raise InfoDeskError(
                "INVALID_TARGET",
                "Only HTTPS URLs for supported source hosts are allowed.",
            ) from error
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self._allowed_hosts
            or parsed.username
            or parsed.password
            or port not in (None, 443)
        ):
            raise InfoDeskError(
                "INVALID_TARGET",
                "Only HTTPS URLs for supported source hosts are allowed.",
            )
