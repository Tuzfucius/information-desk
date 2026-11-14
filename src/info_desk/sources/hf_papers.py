"""Hugging Face Daily Papers adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import (
    AdapterError,
    CandidateSource,
    PageResult,
    SourceInspection,
    bounded_get,
    clean_text,
    now_utc,
    utc_iso,
)

ARXIV_ID = re.compile(r"(?P<id>(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7}))(?:v\d+)?", re.IGNORECASE)
GITHUB_REPO = re.compile(r"^(?:https?://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", re.IGNORECASE)


class HuggingFacePapersAdapter:
    endpoint = "https://huggingface.co/api/daily_papers"
    page_size = 100

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    def normalize(self, raw: dict[str, Any]) -> CandidateSource:
        paper = raw.get("paper") if isinstance(raw.get("paper"), dict) else raw
        raw_id = paper.get("id") or paper.get("arxiv_id") or paper.get("arxivId")
        match = ARXIV_ID.search(str(raw_id or ""))
        if not match:
            raise AdapterError("UPSTREAM_UNAVAILABLE", "Hugging Face paper has no valid arXiv ID.")
        paper_id = match.group("id").lower()
        authors = paper.get("authors") or []
        author_names = [
            clean_text(author.get("name") if isinstance(author, dict) else author, 200)
            for author in authors
        ]
        github_links = _github_links(paper.get("githubRepo") or paper.get("github_repo"))
        description = clean_text(paper.get("summary") or paper.get("abstract"), 800)
        links = [
            {
                "url": url,
                "relation_type": "official_code",
                "evidence_level": "supporting",
                "evidence_origin": "huggingface_daily_papers",
            }
            for url in github_links
        ]
        return CandidateSource(
            source_type="hf_paper",
            source_uid=paper_id,
            external_id=paper_id,
            url=f"https://huggingface.co/papers/{paper_id}",
            title=clean_text(paper.get("title"), 800),
            description=description,
            published_at=utc_iso(
                paper.get("publishedAt")
                or paper.get("published_at")
                or raw.get("publishedAt")
                or raw.get("published_at")
            ),
            source_updated_at=utc_iso(raw.get("updatedAt") or paper.get("updatedAt")),
            metadata={
                "authors": author_names,
                "upvotes": _integer(paper.get("upvotes", raw.get("upvotes"))),
                "github_repos": github_links,
            },
            extracted_links=links,
        )

    async def discover(
        self,
        query: dict[str, Any],
        window: tuple[datetime, datetime],
        page_token: str | None = None,
    ) -> PageResult:
        dates = _days_to_scan(*window)
        page_day, page_number = _decode_token(page_token)
        if page_day >= len(dates):
            raise AdapterError("INVALID_CURSOR", "Hugging Face paper page token is outside the requested window.")
        records_for_window: list[CandidateSource] = []
        current_day = page_day
        current_page = page_number
        while current_day < len(dates):
            response = await bounded_get(
                self.client,
                self.endpoint,
                params={
                    "date": dates[current_day].isoformat(),
                    "p": current_page,
                    "limit": self.page_size,
                    "sort": "publishedAt",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise AdapterError("UPSTREAM_UNAVAILABLE", "Hugging Face Daily Papers returned an invalid response.")
            batch = [self.normalize(item) for item in payload if isinstance(item, dict)]
            records_for_window.extend(
                record
                for record in batch
                if record.published_at is None
                or _in_window(record.published_at, window[0], window[1])
            )
            if len(payload) >= self.page_size:
                next_page = current_page + 1
                return PageResult(
                    records=records_for_window,
                    next_page_token=f"{current_day}:{next_page}",
                    page_complete=False,
                )
            current_day += 1
            current_page = 0
        return PageResult(records=records_for_window, page_complete=True)

    async def inspect(self, target: str) -> SourceInspection:
        paper_id = _paper_id(target)
        response = await bounded_get(
            self.client,
            f"https://huggingface.co/api/papers/{paper_id}",
        )
        if response.status_code == 404:
            raise AdapterError("NOT_FOUND", "Hugging Face paper was not found.")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise AdapterError("UPSTREAM_UNAVAILABLE", "Hugging Face returned an invalid paper record.")
        source = self.normalize(payload)
        return SourceInspection(
            source=source,
            checked_at=now_utc(),
            extracted_links=source.extracted_links,
            limitations=["Hugging Face paper metadata does not independently verify linked code."],
        )


def _github_links(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = [str(item.get("url") or item.get("repo") or "") if isinstance(item, dict) else str(item) for item in value]
    else:
        values = []
    normalized: list[str] = []
    for entry in values:
        match = GITHUB_REPO.match(entry.strip())
        if match:
            normalized.append(f"https://github.com/{match.group(1)}")
    return list(dict.fromkeys(normalized))


def _days_to_scan(start: datetime, end: datetime) -> list[date]:
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    start_day = start.astimezone(UTC).date()
    end_utc = end.astimezone(UTC)
    last_day = end_utc.date()
    if (end_utc.hour, end_utc.minute, end_utc.second, end_utc.microsecond) == (0, 0, 0, 0):
        last_day -= timedelta(days=1)
    days = []
    current = start_day
    while current <= last_day:
        days.append(current)
        current += timedelta(days=1)
    return days


def _decode_token(token: str | None) -> tuple[int, int]:
    if token is None:
        return 0, 0
    try:
        day, page = (int(part) for part in token.split(":", 1))
    except (ValueError, TypeError) as exc:
        raise AdapterError("INVALID_CURSOR", "Invalid Hugging Face paper page token.") from exc
    if day < 0 or page < 0:
        raise AdapterError("INVALID_CURSOR", "Invalid Hugging Face paper page token.")
    return day, page


def _paper_id(target: str) -> str:
    parsed = urlparse(target if "://" in target else f"https://huggingface.co/papers/{target}")
    if parsed.scheme != "https" or parsed.hostname not in {"huggingface.co", "www.huggingface.co"} or parsed.port is not None:
        raise AdapterError("INVALID_TARGET", "Only Hugging Face paper IDs and HTTPS URLs are supported.")
    if not parsed.path.startswith("/papers/"):
        raise AdapterError("INVALID_TARGET", "Target is not a Hugging Face paper.")
    match = ARXIV_ID.search(parsed.path)
    if not match:
        raise AdapterError("INVALID_TARGET", "Invalid Hugging Face paper ID.")
    return match.group("id").lower()


def _in_window(value: str, start: datetime, end: datetime) -> bool:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return start.astimezone(UTC) <= parsed.astimezone(UTC) < end.astimezone(UTC)


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
