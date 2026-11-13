"""Bounded HTTPS client for the supported source adapters."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
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
    ) -> None:
        self.max_response_bytes = max_response_bytes
        self.max_retries = max_retries
        self._last_request: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self.client = httpx.AsyncClient(
            timeout=timeout or httpx.Timeout(connect=5, read=20, write=10, pool=5),
            follow_redirects=False,
            headers={"User-Agent": "information-desk/0.1"},
        )
        self.tokens = {"api.github.com": github_token, "huggingface.co": hf_token}

    async def __aenter__(self) -> SafeHttpClient:
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
        path = urlsplit(url).path
        if host == "export.arxiv.org":
            interval = 3.0
        elif host == "api.github.com" and path.startswith("/search/"):
            interval = 2.1 if self.tokens.get(host) else 6.1
        else:
            interval = 0.1
        async with lock:
            delay = interval - (time.monotonic() - self._last_request.get(host, 0))
            if delay > 0:
                await asyncio.sleep(delay)
            request = self.client.stream(method, url, headers=headers, **kwargs)
            response = await request.__aenter__()
            self._last_request[host] = time.monotonic()
        try:
            yield response
        finally:
            await request.__aexit__(None, None, None)

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        current_url = url
        for _ in range(4):
            self._validate_url(current_url)
            host = urlsplit(current_url).hostname
            headers = dict(kwargs.pop("headers", {}) or {})
            token = self.tokens.get(host or "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                async with self.client.stream("GET", current_url, headers=headers, **kwargs) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            return response
                        current_url = urljoin(current_url, location)
                        continue
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > self.max_response_bytes:
                        raise UpstreamError(
                            "UPSTREAM_UNAVAILABLE",
                            "The source response exceeded the configured size limit.",
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_response_bytes:
                            raise UpstreamError(
                                "UPSTREAM_UNAVAILABLE",
                                "The source response exceeded the configured size limit.",
                            )
                    return httpx.Response(
                        status_code=response.status_code,
                        headers=response.headers,
                        content=bytes(body),
                        request=response.request,
                    )
            except httpx.TimeoutException as error:
                raise UpstreamError("UPSTREAM_TIMEOUT", "The source request timed out.") from error
            except httpx.HTTPError as error:
                raise UpstreamError(
                    "UPSTREAM_UNAVAILABLE", "The source request failed."
                ) from error
        raise InfoDeskError("INVALID_TARGET", "The source redirected too many times.")

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise InfoDeskError(
                "INVALID_TARGET",
                "Only HTTPS URLs for supported source hosts are allowed.",
            )
