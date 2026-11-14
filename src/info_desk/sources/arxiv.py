"""arXiv Atom API adapter."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from .base import (
    AdapterError,
    CandidateSource,
    PageResult,
    SourceCapabilities,
    SourceInspection,
    bounded_get,
    clean_text,
    now_utc,
    parse_window_token,
    utc_iso,
)

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
ARXIV_ID = re.compile(r"(?P<id>(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7}))(?:v\d+)?", re.IGNORECASE)
LINKS = re.compile(r"https?://(?:www\.)?(?:github\.com|huggingface\.co)/[^\s<>\])]+", re.IGNORECASE)


class ArxivAdapter:
    endpoint = "https://export.arxiv.org/api/query"
    capabilities = SourceCapabilities(("arxiv",), window_resolution_seconds=86_400,
                                      title_priorities={"arxiv": 100})

    def __init__(self, client: httpx.AsyncClient, page_size: int = 100):
        self.client = client
        self.page_size = max(1, min(page_size, 200))

    def normalize(self, raw: dict[str, Any]) -> CandidateSource:
        arxiv_id = str(raw.get("id") or "").strip()
        match = ARXIV_ID.search(arxiv_id)
        if not match:
            raise AdapterError("INVALID_TARGET", "Invalid arXiv identifier.")
        source_uid = match.group("id").lower()
        title = clean_text(raw.get("title"))
        summary = clean_text(raw.get("summary"), 800)
        extracted = [
            {"url": url.rstrip(".,;"), "relation_type": "related", "evidence_level": "weak"}
            for url in LINKS.findall(summary)
        ]
        return CandidateSource(
            source_type="arxiv",
            source_uid=source_uid,
            external_id=source_uid,
            url=f"https://arxiv.org/abs/{source_uid}",
            title=title,
            description=summary,
            published_at=utc_iso(raw.get("published")),
            source_updated_at=utc_iso(raw.get("updated")),
            metadata={
                "authors": raw.get("authors", []),
                "categories": raw.get("categories", []),
                "pdf_url": raw.get("pdf_url"),
            },
            extracted_links=extracted,
        )

    async def discover(
        self,
        query: dict[str, Any],
        window: tuple[datetime, datetime],
        page_token: str | None = None,
    ) -> PageResult:
        start = parse_window_token(page_token)
        window_start, window_end = window
        topics = _as_list(query.get("topics"))
        categories = _as_list(query.get("categories"))
        terms = [f'all:"{_escape_query(term)}"' for term in topics]
        category_query = " OR ".join(f"cat:{_escape_query(category)}" for category in categories)
        clauses = []
        if terms:
            clauses.append("(" + " OR ".join(terms) + ")")
        if category_query:
            clauses.append("(" + category_query + ")")
        if not clauses:
            clauses.append("all:*")
        date_clause = (
            f"lastUpdatedDate:[{window_start:%Y%m%d} TO {window_end:%Y%m%d}]"
        )
        search_query = " AND ".join([*clauses, date_clause])
        response = await bounded_get(
            self.client,
            self.endpoint,
            params={
                "search_query": search_query,
                "start": start,
                "max_results": self.page_size,
                "sortBy": "lastUpdatedDate",
                "sortOrder": "ascending",
            },
        )
        response.raise_for_status()
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise AdapterError("UPSTREAM_UNAVAILABLE", "arXiv returned invalid Atom XML.") from exc

        total_node = root.find(f"{ARXIV}totalResults")
        total_results = int(total_node.text or 0) if total_node is not None else None
        records = [self.normalize(_parse_entry(entry)) for entry in root.findall(f"{ATOM}entry")]
        next_start = start + len(records)
        page_complete = not records or (
            total_results is not None and next_start >= total_results
        )
        next_token = None if page_complete else str(next_start)
        return PageResult(
            records=records,
            next_page_token=next_token,
            page_complete=page_complete,
            source_response_incomplete=total_results is None,
            warnings=[] if total_results is not None else ["ARXIV_TOTAL_MISSING"],
        )

    async def inspect(self, target: str) -> SourceInspection:
        arxiv_id = _target_id(target)
        response = await bounded_get(
            self.client,
            self.endpoint,
            params={"id_list": arxiv_id, "max_results": 1},
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        entries = root.findall(f"{ATOM}entry")
        if not entries:
            raise AdapterError("NOT_FOUND", "arXiv paper was not found.")
        source = self.normalize(_parse_entry(entries[0]))
        return SourceInspection(
            source=source,
            checked_at=now_utc(),
            extracted_links=source.extracted_links,
            limitations=["arXiv metadata does not establish code availability or reproducibility."],
        )


def _parse_entry(entry: ElementTree.Element) -> dict[str, Any]:
    identifier = entry.findtext(f"{ATOM}id", "")
    identifier_match = ARXIV_ID.search(identifier)
    if not identifier_match:
        raise AdapterError("UPSTREAM_UNAVAILABLE", "arXiv entry has no valid identifier.")
    authors = [
        clean_text(node.findtext(f"{ATOM}name"), 200)
        for node in entry.findall(f"{ATOM}author")
    ]
    categories = [
        node.attrib.get("term", "")
        for node in entry.findall(f"{ARXIV}primary_category")
    ]
    categories.extend(
        node.attrib.get("term", "")
        for node in entry.findall(f"{ATOM}category")
        if node.attrib.get("term") not in categories
    )
    pdf_link = next(
        (
            node.attrib.get("href")
            for node in entry.findall(f"{ATOM}link")
            if node.attrib.get("title") == "pdf" or node.attrib.get("type") == "application/pdf"
        ),
        None,
    )
    return {
        "id": identifier_match.group("id"),
        "title": entry.findtext(f"{ATOM}title", ""),
        "summary": entry.findtext(f"{ATOM}summary", ""),
        "published": entry.findtext(f"{ATOM}published"),
        "updated": entry.findtext(f"{ATOM}updated"),
        "authors": authors,
        "categories": categories,
        "pdf_url": pdf_link,
    }


def _target_id(target: str) -> str:
    parsed = urlparse(target if "://" in target else f"https://arxiv.org/abs/{target}")
    if parsed.scheme != "https" or parsed.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"} or parsed.port is not None:
        raise AdapterError("INVALID_TARGET", "Only official arXiv identifiers and HTTPS URLs are supported.")
    match = ARXIV_ID.search(parsed.path)
    if not match:
        raise AdapterError("INVALID_TARGET", "Invalid arXiv identifier.")
    return match.group("id").lower()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not value:
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _escape_query(value: str) -> str:
    return re.sub(r'["\\{}]', " ", value).strip()
