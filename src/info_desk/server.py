"""MCP stdio adapter for the application service."""

from __future__ import annotations

import inspect
import logging
from datetime import datetime
from typing import Annotated, Any, Literal, TypeVar

from mcp.server import MCPServer
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from info_desk.contracts import (
    EventsRequest,
    EventsResponse,
    FetchRequest,
    FetchResponse,
    InspectRequest,
    InspectResponse,
    SaveRequest,
    SaveResponse,
    SearchRequest,
    SearchResponse,
    StatusRequest,
    StatusResponse,
)
from info_desk.exceptions import InfoDeskError

logger = logging.getLogger(__name__)
ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


def create_server(service: Any, *, profile: str = "read-only") -> MCPServer:
    if profile not in {"read-only", "standard"}:
        raise ValueError("profile must be 'read-only' or 'standard'")

    server = MCPServer("information-desk", version="0.1.0")

    async def response_for(
        response_type: type[ResponseModel],
        operation: Any,
    ) -> ResponseModel:
        try:
            value = operation()
            if inspect.isawaitable(value):
                value = await value
            value = dict(value) if isinstance(value, dict) else value
            warnings = value.pop("warnings", []) if isinstance(value, dict) else []
        except InfoDeskError as error:
            details = {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
                "retry_after_seconds": error.retry_after_seconds,
                "details": error.details,
            }
            return response_type.model_validate({"ok": False, "error": details, "warnings": []})
        except PydanticValidationError:
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
        try:
            return response_type.model_validate(
                {"ok": True, "data": value, "warnings": warnings}
            )
        except PydanticValidationError:
            logger.exception("MCP service result did not satisfy its output schema")
            return response_type.model_validate(
                {
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "The service returned data outside the tool output schema.",
                        "retryable": False,
                        "details": {},
                    },
                    "warnings": [],
                }
            )

    @server.tool()
    async def search_items(
        item_id: Annotated[int, Field(gt=0)] | None = None,
        keyword: Annotated[str, Field(min_length=1, max_length=200)] | None = None,
        category: Annotated[str, Field(min_length=1, max_length=64)] | None = None,
        triage_status: Literal["inbox", "saved", "ignored"] | None = None,
        is_watched: bool | None = None,
        code_status: Literal["unknown", "not_detected", "detected"] | None = None,
        source_type: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")] | None = None,
        published_from: datetime | None = None,
        published_before: datetime | None = None,
        first_seen_after: datetime | None = None,
        updated_after: datetime | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        cursor: Annotated[str, Field(max_length=512)] | None = None,
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
        after_id: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
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
        feeds: list[Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]] | None = None,
    ) -> StatusResponse:
        """Read local synchronization coverage and recent run status."""
        return await response_for(
            StatusResponse, lambda: service.get_sync_status(StatusRequest(feeds=feeds))
        )

    if profile == "standard":

        @server.tool()
        async def inspect_source(
            target: Annotated[str, Field(min_length=1, max_length=2048)]
        ) -> InspectResponse:
            """Inspect a supported raw arXiv ID or official arXiv, HF, or GitHub HTTPS URL."""
            return await response_for(
                InspectResponse,
                lambda: service.inspect_source(InspectRequest(target=target)),
            )

        @server.tool()
        async def save_item(
            item_id: Annotated[int, Field(gt=0)] | None = None,
            source: dict[str, str] | None = None,
            expected_revision: Annotated[int, Field(ge=0)] | None = None,
            triage_status: Literal["inbox", "saved", "ignored"] | None = None,
            category: Annotated[str, Field(max_length=64)] | None = None,
            is_watched: bool | None = None,
            notes: Annotated[str, Field(max_length=4000)] | None = None,
        ) -> SaveResponse:
            """Save, ignore, follow, or annotate an item, or import a verified source."""
            return await response_for(
                SaveResponse,
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
            days: Annotated[int, Field(ge=1, le=14)] = 2,
            feeds: list[Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]] | None = None,
            topics: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = None,
            limit: Annotated[int, Field(ge=1, le=100)] = 30,
        ) -> FetchResponse:
            """Run a bounded sync now. This contacts sources and writes new records locally."""
            return await response_for(
                FetchResponse,
                lambda: service.fetch_updates(
                    FetchRequest(days=days, feeds=feeds, topics=topics, limit=limit)
                ),
            )

    return server


def serve(service: Any, *, profile: str = "read-only") -> None:
    create_server(service, profile=profile).run(transport="stdio")
