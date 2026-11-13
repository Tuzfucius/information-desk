"""Domain values shared by adapters, services and persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Feed = Literal["arxiv", "hf_papers", "hf_models", "github"]
SourceType = Literal["arxiv", "hf_paper", "hf_model", "github_repo"]
TriageStatus = Literal["inbox", "saved", "ignored"]
CodeStatus = Literal["unknown", "not_detected", "detected"]
EventType = Literal["discovered", "source_attached", "content_changed", "code_detected"]


class SourceRef(BaseModel):
    source_type: SourceType
    source_uid: str
    external_id: str
    url: str
    published_at: datetime | None = None
    source_updated_at: datetime | None = None


class ItemView(BaseModel):
    item_id: int
    revision: int
    title: str
    summary: str
    category: str
    triage_status: TriageStatus
    is_watched: bool
    code_status: CodeStatus
    first_seen_at: datetime
    updated_at: datetime
    sources: list[SourceRef] = Field(default_factory=list)
    sources_truncated: bool = False
    notes: str | None = None


class EventView(BaseModel):
    id: int
    item_id: int
    event_type: EventType
    created_at: datetime
    source: SourceRef | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class WarningView(BaseModel):
    code: str
    message: str
    feed: Feed | None = None


class ErrorView(BaseModel):
    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None
    details: dict[str, object] = Field(default_factory=dict)


class ToolResponse(BaseModel):
    ok: bool
    data: dict[str, object] | None = None
    error: ErrorView | None = None
    warnings: list[WarningView] = Field(default_factory=list)
