"""Hugging Face Hub models adapter."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote, unquote, urlparse

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

CODE_FILE = re.compile(
    r"\.(?:py|pyi|ipynb|js|ts|tsx|jsx|rs|go|java|cpp|c|h|hpp|sh|yaml|yml|toml)$",
    re.IGNORECASE,
)
README_LIMIT = 4000
RESPONSE_LIMIT = 4_000_000


class HuggingFaceModelsAdapter:
    endpoint = "https://huggingface.co/api/models"

    def __init__(self, client: httpx.AsyncClient, page_size: int = 100):
        self.client = client
        self.page_size = max(1, min(page_size, 100))

    def normalize(self, raw: dict[str, Any]) -> CandidateSource:
        repo_id = str(raw.get("id") or raw.get("modelId") or "").strip()
        if not repo_id or "/" not in repo_id:
            raise AdapterError("UPSTREAM_UNAVAILABLE", "Hugging Face model has no repository ID.")
        tags = raw.get("tags") or []
        card_data = raw.get("cardData") or raw.get("card_data") or {}
        return CandidateSource(
            source_type="hf_model",
            source_uid=repo_id.lower(),
            external_id=repo_id,
            url=f"https://huggingface.co/{repo_id}",
            title=clean_text(card_data.get("title") or repo_id, 800),
            description=clean_text(card_data.get("description") or raw.get("description"), 800),
            published_at=utc_iso(raw.get("createdAt") or raw.get("created_at")),
            source_updated_at=utc_iso(raw.get("lastModified") or raw.get("last_modified")),
            metadata={
                "likes": _integer(raw.get("likes")),
                "downloads": _integer(raw.get("downloads")),
                "pipeline_tag": raw.get("pipeline_tag"),
                "tags": tags[:100] if isinstance(tags, list) else [],
                "library_name": raw.get("library_name"),
                "private": raw.get("private"),
            },
            extracted_links=_paper_links(card_data, tags),
        )

    async def discover(
        self,
        query: dict[str, Any],
        window: tuple[datetime, datetime],
        page_token: str | None = None,
    ) -> PageResult:
        params: dict[str, Any] = {
            "limit": self.page_size,
            "sort": "lastModified",
            "direction": "-1",
            "full": "true",
            "cardData": "true",
        }
        topic = query.get("topic")
        if topic:
            params["search"] = str(topic)[:100]
        if query.get("filter"):
            params["filter"] = str(query["filter"])[:100]
        if page_token:
            params["cursor"] = page_token
        response = await bounded_get(self.client, self.endpoint, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise AdapterError("UPSTREAM_UNAVAILABLE", "Hugging Face model listing returned an invalid response.")
        records = []
        start, end = window
        oldest_timestamp: str | None = None
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            candidate = self.normalize(raw)
            updated = candidate.source_updated_at
            if updated is not None:
                oldest_timestamp = updated
            if updated is not None and not _in_window(updated, start, end):
                continue
            records.append(candidate)
        next_token = _next_cursor(response.headers.get("Link"))
        old_enough = oldest_timestamp is not None and _before(oldest_timestamp, start)
        if old_enough:
            next_token = None
        incomplete = len(payload) >= self.page_size and next_token is None and not old_enough
        warnings = ["HF_MODEL_CURSOR_MISSING"] if incomplete else []
        if payload and oldest_timestamp is None and next_token is None:
            incomplete = True
            warnings = ["HF_MODEL_TIMESTAMP_MISSING"]
        return PageResult(
            records=records,
            next_page_token=next_token,
            page_complete=next_token is None and not incomplete,
            source_response_incomplete=incomplete,
            warnings=warnings,
        )

    async def inspect(self, target: str) -> SourceInspection:
        repo_id = _model_id(target)
        response = await bounded_get(self.client, f"{self.endpoint}/{quote(repo_id, safe='/')}")
        if response.status_code == 404:
            raise AdapterError("NOT_FOUND", "Hugging Face model was not found.")
        response.raise_for_status()
        raw = response.json()
        source = self.normalize(raw)
        tree_response = await bounded_get(
            self.client,
            f"https://huggingface.co/api/models/{quote(repo_id, safe='/')}/tree/main",
            params={"recursive": "false", "expand": "false"},
        )
        root_files: list[str] = []
        signals: list[str] = []
        excerpt: str | None = None
        code_status = "unknown"
        limitations: list[str] = []
        if tree_response.status_code == 403:
            limitations.append("Model files are gated or unavailable to this client.")
        elif tree_response.status_code == 404:
            limitations.append("Model repository tree was not found.")
        else:
            tree_response.raise_for_status()
            tree = tree_response.json()
            if isinstance(tree, list):
                root_files = [
                    str(entry.get("path", ""))
                    for entry in tree
                    if isinstance(entry, dict) and entry.get("path")
                ][:100]
                code_files = [name for name in root_files if CODE_FILE.search(name)]
                readme_name = next((name for name in root_files if name.lower() == "readme.md"), None)
                if code_files:
                    code_status = "detected"
                    signals.extend(f"file:{name}" for name in code_files[:10])
                else:
                    code_status = "not_detected"
                if readme_name:
                    readme = await bounded_get(
                        self.client,
                        f"https://huggingface.co/{quote(repo_id, safe='/')}/raw/main/{quote(readme_name)}",
                        headers={"Range": f"bytes=0-{RESPONSE_LIMIT - 1}"},
                        max_bytes=RESPONSE_LIMIT,
                    )
                    if 200 <= readme.status_code < 300:
                        text = readme.text[:RESPONSE_LIMIT]
                        excerpt = text[:README_LIMIT]
                        if len(text) > README_LIMIT:
                            limitations.append("Model card excerpt truncated to 4000 characters.")
                    elif readme.status_code in {401, 403}:
                        limitations.append("Model card is gated or inaccessible.")
                    else:
                        limitations.append("Model card could not be read.")
        return SourceInspection(
            source=source,
            checked_at=now_utc(),
            code_status=code_status,
            signals=signals,
            root_files=root_files,
            checked_depth=1 if root_files else 0,
            extracted_links=source.extracted_links,
            content_excerpt=excerpt,
            limitations=limitations,
        )


def _model_id(target: str) -> str:
    parsed = urlparse(target if "://" in target else f"https://huggingface.co/{target}")
    if parsed.scheme != "https" or parsed.hostname not in {"huggingface.co", "www.huggingface.co"} or parsed.port is not None:
        raise AdapterError("INVALID_TARGET", "Only Hugging Face model IDs and HTTPS URLs are supported.")
    path = unquote(parsed.path).strip("/")
    if path.startswith(("papers/", "datasets/", "spaces/")) or path.count("/") < 1:
        raise AdapterError("INVALID_TARGET", "Target is not a Hugging Face model repository.")
    parts = path.split("/")
    if len(parts) == 2 and all(parts):
        return path
    if len(parts) >= 4 and parts[2] in {"tree", "blob"} and parts[3]:
        return "/".join(parts[:2])
    raise AdapterError("INVALID_TARGET", "Target must identify a Hugging Face model repository.")


def _paper_links(card_data: dict[str, Any], tags: list[str]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    paper_ids = card_data.get("paper", {}).get("id") if isinstance(card_data.get("paper"), dict) else None
    if paper_ids:
        links.append({
            "url": f"https://arxiv.org/abs/{paper_ids}",
            "relation_type": "official_model",
            "evidence_level": "supporting",
            "evidence_origin": "huggingface_model_card",
        })
    for tag in tags:
        value = str(tag)
        if value.startswith("arxiv:"):
            paper_id = value.partition(":")[2]
            if paper_id:
                links.append({
                    "url": f"https://arxiv.org/abs/{paper_id}",
                    "relation_type": "official_model",
                    "evidence_level": "supporting",
                    "evidence_origin": "huggingface_model_tag",
                })
    return links


def _next_cursor(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' not in part:
            continue
        match = re.search(r"<([^>]+)>", part)
        if not match:
            continue
        parsed = urlparse(match.group(1))
        if parsed.scheme != "https" or parsed.hostname != "huggingface.co":
            return None
        from urllib.parse import parse_qs

        return parse_qs(parsed.query).get("cursor", [None])[0]
    return None


def _in_window(value: str, start: datetime, end: datetime) -> bool:
    from datetime import UTC

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return start.astimezone(UTC) <= parsed.astimezone(UTC) < end.astimezone(UTC)


def _before(value: str, boundary: datetime) -> bool:
    from datetime import UTC

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if boundary.tzinfo is None:
        boundary = boundary.replace(tzinfo=UTC)
    return parsed.astimezone(UTC) < boundary.astimezone(UTC)


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
