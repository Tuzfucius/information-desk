"""Shared source adapter data and parsing helpers."""

from __future__ import annotations

import re
import asyncio
import random
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx


@dataclass(slots=True)
class CandidateSource:
    source_type: str
    source_uid: str
    external_id: str
    url: str
    title: str
    description: str = ""
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


class SourceAdapter(Protocol):
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

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
                    headers=streamed.headers,
                    content=bytes(body),
                    request=streamed.request,
                    extensions=streamed.extensions,
                )
            if response.status_code != 429 and response.status_code < 500:
                return response
            if attempt == retries:
                return response
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 0.0
            except ValueError:
                delay = 0.0
            if delay <= 0:
                delay = min(8.0, 0.5 * (2**attempt)) * random.uniform(0.75, 1.25)
            await asyncio.sleep(delay)
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == retries:
                raise
            delay = min(8.0, 0.5 * (2**attempt)) * random.uniform(0.75, 1.25)
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable retry state")


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
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
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
