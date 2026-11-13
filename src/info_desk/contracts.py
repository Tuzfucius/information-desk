"""Validated public requests and typed MCP results."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from info_desk.domain import ErrorView, EventView, Feed, ItemView, WarningView


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchRequest(StrictModel):
    item_id: int | None = Field(default=None, gt=0)
    keyword: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, min_length=1, max_length=64)
    triage_status: Literal["inbox", "saved", "ignored"] | None = None
    is_watched: bool | None = None
    code_status: Literal["unknown", "not_detected", "detected"] | None = None
    source_type: Literal["arxiv", "hf_paper", "hf_model", "github_repo"] | None = None
    published_from: datetime | None = None
    published_before: datetime | None = None
    first_seen_after: datetime | None = None
    updated_after: datetime | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None

    @field_validator(
        "published_from",
        "published_before",
        "first_seen_after",
        "updated_after",
    )
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone offset")
        return value

    @field_validator("keyword", "category")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("text filters cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_query(self) -> SearchRequest:
        if self.item_id is not None and any(
            value is not None
            for value in (
                self.keyword,
                self.category,
                self.triage_status,
                self.is_watched,
                self.code_status,
                self.source_type,
                self.published_from,
                self.published_before,
                self.first_seen_after,
                self.updated_after,
                self.cursor,
            )
        ):
            raise ValueError("item_id must be used as an exact query")
        if self.published_from and self.published_before:
            if self.published_from >= self.published_before:
                raise ValueError("published_from must be earlier than published_before")
        return self


class EventsRequest(StrictModel):
    after_id: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)
    event_types: list[
        Literal["discovered", "source_attached", "content_changed", "code_detected"]
    ] | None = None
    include_ignored: bool = False
    only_watched: bool = False


class StatusRequest(StrictModel):
    feeds: list[Feed] | None = None

    @field_validator("feeds")
    @classmethod
    def validate_feeds(cls, value: list[Feed] | None) -> list[Feed] | None:
        if value is not None and (not value or len(value) != len(set(value))):
            raise ValueError("feeds must contain unique values")
        return value


class InspectRequest(StrictModel):
    target: str = Field(min_length=1, max_length=2048)


class SaveRequest(StrictModel):
    item_id: int | None = Field(default=None, gt=0)
    source: dict[str, str] | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    triage_status: Literal["inbox", "saved", "ignored"] | None = None
    category: str | None = Field(default=None, max_length=64)
    is_watched: bool | None = None
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("category cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_save(self) -> SaveRequest:
        if (self.item_id is None) == (self.source is None):
            raise ValueError("provide exactly one of item_id or source")
        if self.item_id is not None and self.expected_revision is None:
            raise ValueError("expected_revision is required when updating an item")
        if self.source is not None and self.expected_revision is not None:
            raise ValueError("expected_revision is not valid when importing a source")
        if self.item_id is not None and all(
            value is None
            for value in (
                self.triage_status,
                self.category,
                self.is_watched,
                self.notes,
            )
        ):
            raise ValueError("at least one item field must be provided")
        if self.source is not None and set(self.source) != {"target"}:
            raise ValueError("source must contain only the target field")
        return self


class FetchRequest(StrictModel):
    days: int = Field(default=2, ge=1, le=14)
    feeds: list[Feed] | None = None
    topics: list[str] | None = None
    limit: int = Field(default=30, ge=1, le=100)

    @model_validator(mode="after")
    def validate_topics(self) -> FetchRequest:
        if self.feeds is not None and (not self.feeds or len(set(self.feeds)) != len(self.feeds)):
            raise ValueError("feeds must contain unique values")
        if self.topics is not None:
            if not 1 <= len(self.topics) <= 10:
                raise ValueError("topics must contain 1 to 10 values")
            if any(not topic.strip() or len(topic) > 100 for topic in self.topics):
                raise ValueError("each topic must contain 1 to 100 non-space characters")
        return self


class SearchData(BaseModel):
    items: list[ItemView]
    next_cursor: str | None = None


class SearchResponse(BaseModel):
    ok: bool
    data: SearchData | None = None
    error: ErrorView | None = None
    warnings: list[WarningView] = Field(default_factory=list)


class EventsData(BaseModel):
    events: list[EventView]
    next_after_id: int
    has_more: bool


class EventsResponse(BaseModel):
    ok: bool
    data: EventsData | None = None
    error: ErrorView | None = None
    warnings: list[WarningView] = Field(default_factory=list)


class ObjectResponse(BaseModel):
    ok: bool
    data: dict[str, object] | None = None
    error: ErrorView | None = None
    warnings: list[WarningView] = Field(default_factory=list)
