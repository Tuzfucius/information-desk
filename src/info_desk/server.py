"""MCP stdio adapter for the application service."""

from __future__ import annotations

import inspect
import logging
from datetime import datetime
from typing import Any
from typing import Literal

from mcp.server import MCPServer
from pydantic import ValidationError as PydanticValidationError

from info_desk.contracts import (
    EventsRequest,
    EventsResponse,
    FetchRequest,
    InspectRequest,
    ObjectResponse,
    SaveRequest,
    SearchRequest,
    SearchResponse,
    StatusRequest,
)
from info_desk.exceptions import InfoDeskError

logger = logging.getLogger(__name__)


def create_server(service: Any, *, profile: str = "read-only") -> MCPServer:
    if profile not in {"read-only", "standard"}:
        raise ValueError("profile must be 'read-only' or 'standard'")

    server = MCPServer("information-desk", version="0.1.0")

    async def response_for(
        response_type: type[ObjectResponse] | type[SearchResponse] | type[EventsResponse],
        operation: Any,
    ) -> Any:
        try:
            value = operation()
            if inspect.isawaitable(value):
                value = await value
            warnings = value.pop("warnings", []) if isinstance(value, dict) else []
            return response_type.model_validate(
                {"ok": True, "data": value, "warnings": warnings}
            )
        except InfoDeskError as error:
            details = {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
                "retry_after_seconds": error.retry_after_seconds,
                "details": error.details,
            }
            return response_type.model_validate({"ok": False, "error": details, "warnings": []})
        except PydanticValidationError as error:
            return response_type.model_validate(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_ARGUMENT",
                        "message": "Request parameters did not satisfy the tool schema.",
                        "retryable": False,
                        "details": {},
                    },
                    "warnings": [],
                }
            )
        except Exception:
            logger.exception("Unhandled MCP tool failure")
            return response_type.model_validate(
                {
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "The service could not complete this request.",
                        "retryable": False,
                        "details": {},
                    },
                    "warnings": [],
                }
            )

    @server.tool()
    async def search_items(
        item_id: int | None = None,
        keyword: str | None = None,
        category: str | None = None,
        triage_status: Literal["inbox", "saved", "ignored"] | None = None,
        is_watched: bool | None = None,
        code_status: Literal["unknown", "not_detected", "detected"] | None = None,
        source_type: Literal["arxiv", "hf_paper", "hf_model", "github_repo"] | None = None,
        published_from: datetime | None = None,
        published_before: datetime | None = None,
        first_seen_after: datetime | None = None,
        updated_after: datetime | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> SearchResponse:
        """Search the local research catalogue. This tool does not contact source websites."""
        return await response_for(
            SearchResponse,
            lambda: service.search_items(
                SearchRequest(
                    item_id=item_id,
                    keyword=keyword,
                    category=category,
                    triage_status=triage_status,
                    is_watched=is_watched,
                    code_status=code_status,
                    source_type=source_type,
                    published_from=published_from,
                    published_before=published_before,
                    first_seen_after=first_seen_after,
                    updated_after=updated_after,
                    limit=limit,
                    cursor=cursor,
                )
            ),
        )

    @server.tool()
    async def list_events(
        after_id: int = 0,
        limit: int = 50,
        event_types: list[
            Literal["discovered", "source_attached", "content_changed", "code_detected"]
        ] | None = None,
        include_ignored: bool = False,
        only_watched: bool = False,
    ) -> EventsResponse:
        """Read durable changes in ascending event ID order."""
        return await response_for(
            EventsResponse,
            lambda: service.list_events(
                EventsRequest(
                    after_id=after_id,
                    limit=limit,
                    event_types=event_types,
                    include_ignored=include_ignored,
                    only_watched=only_watched,
                )
            ),
        )

    @server.tool()
    async def get_sync_status(
        feeds: list[Literal["arxiv", "hf_papers", "hf_models", "github"]] | None = None,
    ) -> ObjectResponse:
        """Read local synchronization coverage and recent run status."""
        return await response_for(
            ObjectResponse, lambda: service.get_sync_status(StatusRequest(feeds=feeds))
        )

    if profile == "standard":

        @server.tool()
        async def inspect_source(target: str) -> ObjectResponse:
            """Inspect a supported arXiv, Hugging Face, or GitHub source over HTTPS."""
            return await response_for(
                ObjectResponse,
                lambda: service.inspect_source(InspectRequest(target=target)),
            )

        @server.tool()
        async def save_item(
            item_id: int | None = None,
            source: dict[str, str] | None = None,
            expected_revision: int | None = None,
            triage_status: Literal["inbox", "saved", "ignored"] | None = None,
            category: str | None = None,
            is_watched: bool | None = None,
            notes: str | None = None,
        ) -> ObjectResponse:
            """Save, ignore, follow, or annotate an item, or import a verified source."""
            return await response_for(
                ObjectResponse,
                lambda: service.save_item(
                    SaveRequest(
                        item_id=item_id,
                        source=source,
                        expected_revision=expected_revision,
                        triage_status=triage_status,
                        category=category,
                        is_watched=is_watched,
                        notes=notes,
                    )
                ),
            )

        @server.tool()
        async def fetch_updates(
            days: int = 2,
            feeds: list[Literal["arxiv", "hf_papers", "hf_models", "github"]] | None = None,
            topics: list[str] | None = None,
            limit: int = 30,
        ) -> ObjectResponse:
            """Run a bounded sync now. This contacts sources and writes new records locally."""
            return await response_for(
                ObjectResponse,
                lambda: service.fetch_updates(
                    FetchRequest(days=days, feeds=feeds, topics=topics, limit=limit)
                ),
            )

    return server


def serve(service: Any, *, profile: str = "read-only") -> None:
    create_server(service, profile=profile).run(transport="stdio")
