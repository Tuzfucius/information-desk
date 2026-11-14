"""Shared source adapter data and parsing helpers."""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol

import httpx


@dataclass(slots=True)
class CandidateSource:
    source_type: str
    source_uid: str
    external_id: str
    url: str
    title: str
    description: str = ""
    feed: str | None = None
    watch_interval_hours: int | None = None
    published_at: str | None = None
    source_updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    extracted_links: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PageResult:
    records: list[CandidateSource]
    next_page_token: str | None = None
    page_complete: bool = True
    source_response_incomplete: bool = False
    warnings: list[str] = field(default_factory=list)
    resume_safe: bool = False
    checkpoint_token: str | None = None


@dataclass(slots=True)
class SourceInspection:
    source: CandidateSource
    checked_at: str
    code_status: str = "unknown"
    signals: list[str] = field(default_factory=list)
    root_files: list[str] = field(default_factory=list)
    checked_depth: int = 0
    extracted_links: list[dict[str, Any]] = field(default_factory=list)
    content_excerpt: str | None = None
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source"] = self.source.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    """Scheduling and discovery guarantees declared by one feed adapter."""

    source_types: tuple[str, ...]
    discovery_mode: Literal["window", "cursor", "poll", "none"] = "window"
    window_resolution_seconds: int | None = 86_400
    window_filtering: Literal["server", "client", "none"] = "server"
    durable_cursor: bool = False
    supports_inspect: bool = True
    target_matcher: Callable[[str], bool] | None = None
    request_hosts: tuple[str, ...] = ()
    min_request_interval_seconds: float = 0.1
    watch_interval_hours: int = 24
    watch_timeout_seconds: float = 20.0
    title_priorities: dict[str, int] = field(default_factory=dict)
    query_builder: Callable[[list[str]], list[dict[str, Any]]] | None = None

    @property
    def supports_time_coverage(self) -> bool:
        return (
            self.discovery_mode == "window"
            and self.window_filtering in {"server", "client"}
        )


class SourceAdapter(Protocol):
    capabilities: SourceCapabilities

    async def discover(
        self,
        query: dict[str, Any],
        window: tuple[datetime, datetime],
        page_token: str | None = None,
    ) -> PageResult: ...

    async def inspect(self, target: str) -> SourceInspection: ...

    def normalize(self, raw: dict[str, Any]) -> CandidateSource: ...


class AdapterError(RuntimeError):
    """An expected source or input error with a stable application error code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


async def bounded_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int = 4_000_000,
    **kwargs: Any,
) -> httpx.Response:
    """Read a response with a hard decoded-body size limit."""
    max_bytes = min(max_bytes, int(getattr(client, "max_response_bytes", max_bytes)))
    retries = max(0, int(getattr(client, "max_retries", 2)))
    for attempt in range(retries + 1):
        try:
            async with client.stream("GET", url, **kwargs) as streamed:
                content_length = streamed.headers.get("Content-Length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    raise AdapterError(
                        "UPSTREAM_RESPONSE_TOO_LARGE",
                        "Source response exceeded the configured size limit.",
                    )
                body = bytearray()
                async for chunk in streamed.aiter_bytes():
                    if len(body) + len(chunk) > max_bytes:
                        raise AdapterError(
                            "UPSTREAM_RESPONSE_TOO_LARGE",
                            "Source response exceeded the configured size limit.",
                        )
                    body.extend(chunk)
                response = httpx.Response(
                    status_code=streamed.status_code,
                    headers=_decoded_headers(streamed.headers),
                    content=bytes(body),
                    request=streamed.request,
                    extensions=streamed.extensions,
                )
            retryable_403 = response.status_code == 403 and (
                response.headers.get("Retry-After") is not None
                or response.headers.get("X-RateLimit-Remaining") == "0"
                or b"secondary rate limit" in response.content.lower()
            )
            retryable = (
                response.status_code == 429
                or response.status_code >= 500
                or retryable_403
            )
            if not retryable:
                return response
            if attempt == retries:
                return response
            await _sleep(client, _retry_delay(response, attempt, client))
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == retries:
                raise
            delay = min(8.0, 0.5 * (2**attempt)) * random.uniform(0.75, 1.25)
            await _sleep(client, delay)
    raise RuntimeError("unreachable retry state")


def _decoded_headers(headers: httpx.Headers) -> httpx.Headers:
    """Drop wire-body metadata after streamed bytes have been decoded."""
    decoded = httpx.Headers(headers)
    for name in ("content-encoding", "content-length", "transfer-encoding"):
        if name in decoded:
            del decoded[name]
    return decoded


async def _sleep(client: object, delay: float) -> None:
    sleeper = getattr(client, "sleep", asyncio.sleep)
    await sleeper(delay)


def _retry_delay(response: httpx.Response, attempt: int, client: object) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, retry_at.timestamp() - getattr(client, "clock", time.time)())
            except (TypeError, ValueError, OverflowError):
                pass
    reset = response.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(0.0, float(reset) - getattr(client, "clock", time.time)())
        except ValueError:
            pass
    return min(8.0, 0.5 * (2**attempt)) * random.uniform(0.75, 1.25)


def utc_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_text(value: Any, limit: int = 800) -> str:
    if value is None:
        return ""
    text = re.sub(r"<[^>]*>", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_window_token(token: str | None, default: int = 0) -> int:
    if token is None:
        return default
    try:
        value = int(token)
    except (TypeError, ValueError) as exc:
        raise AdapterError("INVALID_CURSOR", "Invalid source page token.") from exc
    if value < 0:
        raise AdapterError("INVALID_CURSOR", "Invalid source page token.")
    return value


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
