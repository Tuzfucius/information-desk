"""Application use cases shared by the CLI and MCP server."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from info_desk.config import Settings
from info_desk.contracts import (
    EventsRequest,
    FetchRequest,
    InspectRequest,
    SaveRequest,
    SearchRequest,
    StatusRequest,
)
from info_desk.domain import EventView, ItemView
from info_desk.exceptions import (
    InfoDeskError,
    NotFoundError,
    RevisionConflictError,
    UpstreamError,
)
from info_desk.inspect import SourceInspector
from info_desk.resolver import IdentityResolver
from info_desk.sources.base import CandidateSource
from info_desk.sources.registry import AdapterRegistry, create_default_registry
from info_desk.storage.repository import Repository
from info_desk.sync_engine import SyncEngine
from info_desk.watch_refresh import WatchRefresher


class InformationService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        http_client: Any,
        adapters: AdapterRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.http_client = http_client
        self.adapters = adapters or create_default_registry(http_client, settings.github_token)
        register_policy = getattr(http_client, "register_source_policy", None)
        if register_policy is not None:
            for feed in self.adapters:
                capabilities = self.adapters.capabilities(feed)
                register_policy(
                    request_hosts=capabilities.request_hosts,
                    min_interval_seconds=capabilities.min_request_interval_seconds,
                )
        priorities = {
            source_type: priority
            for feed in self.adapters
            for source_type, priority in self.adapters.capabilities(feed).title_priorities.items()
        }
        self.resolver = IdentityResolver(repository, priorities)
        self.watch_refresher = WatchRefresher(
            settings, repository, self.adapters, self.resolver,
            upstream_error=self._upstream_error,
        )
        self.source_inspector = SourceInspector(self.adapters, self._upstream_error)
        self.sync_engine = SyncEngine(
            settings,
            repository,
            self.adapters,
            self.resolver,
            self.watch_refresher,
            self._upstream_error,
        )

    async def search_items(self, request: SearchRequest) -> dict[str, Any]:
        if request.item_id is not None:
            item = self.repository.get_item(request.item_id)
            if item is None:
                raise NotFoundError(f"Item {request.item_id} was not found.")
            return {"items": [self._item_view(item, include_notes=True)], "next_cursor": None}

        filters = request.model_dump(exclude={"cursor"}, mode="json")
        before = None
        if request.cursor:
            before = self._decode_cursor(request.cursor, filters)
        include_ignored = request.triage_status == "ignored"
        page = self.repository.search_items_page(
            item_id=None,
            keyword=request.keyword,
            category=request.category,
            triage_status=request.triage_status,
            include_ignored=include_ignored,
            is_watched=request.is_watched,
            code_status=request.code_status,
            source_type=request.source_type,
            published_from=self._utc_string(request.published_from),
            published_before=self._utc_string(request.published_before),
            first_seen_after=self._utc_string(request.first_seen_after),
            updated_after=self._utc_string(request.updated_after),
            limit=request.limit,
            before=before,
        )
        items = [self._item_view(item) for item in page["items"]]
        next_cursor = None
        if page["has_more"] and items:
            last = page["items"][-1]
            next_cursor = self._encode_cursor(
                filters, (last["updated_at"], int(last["item_id"]))
            )
        return {"items": items, "next_cursor": next_cursor}

    def list_events(self, request: EventsRequest) -> dict[str, Any]:
        result = self.repository.list_events(
            after_id=request.after_id,
            limit=request.limit,
            event_types=request.event_types,
            include_ignored=request.include_ignored,
            only_watched=request.only_watched,
        )
        return {
            **result,
            "events": [EventView.model_validate(event) for event in result["events"]],
        }

    def get_sync_status(self, request: StatusRequest) -> dict[str, Any]:
        feeds = request.feeds
        states = self.repository.list_sync_states(feeds=feeds)
        if feeds is None:
            feeds = sorted(
                set(self.settings.enabled_feeds)
                | set(self.adapters)
                | {state["feed"] for state in states}
            )
        jobs = []
        for state in states:
            latest_run = self.repository.list_runs(job_key=state["job_key"], limit=1)
            status = (
                "running"
                if latest_run and latest_run[0]["status"] == "running"
                else state["last_status"]
            )
            jobs.append(
                {
                    "job_key": state["job_key"],
                    "feed": state["feed"],
                    "query": state["query"],
                    "coverage_from": state["coverage_from"],
                    "completed_until": state["completed_until"],
                    "last_attempt_at": state["last_attempt_at"],
                    "last_success_at": state["last_success_at"],
                    "last_status": status,
                    "last_error_code": state["last_error"] or None,
                    "pending_segments": len(
                        self.repository.list_sync_segments(state["job_key"])
                    ),
                }
            )
        represented = {state["feed"] for state in states}
        jobs.extend(
            {
                "job_key": f"never:{feed}",
                "feed": feed,
                "query": {},
                "coverage_from": None,
                "completed_until": None,
                "last_attempt_at": None,
                "last_success_at": None,
                "last_status": "never",
                "last_error_code": None,
                "pending_segments": 0,
            }
            for feed in feeds
            if feed not in represented
        )
        watch_runs = self.repository.list_runs(job_key="watch_refresh", limit=1)
        latest_watch = watch_runs[0] if watch_runs else None
        watch_queue = self.repository.watch_queue_status(self._now())
        pending_due = watch_queue["due_count"] > 0
        return {
            "jobs": jobs,
            "watch_refresh": {
                "last_attempt_at": latest_watch["started_at"] if latest_watch else None,
                "last_status": latest_watch["status"] if latest_watch else "never",
                "checked": latest_watch["fetched_count"] if latest_watch else None,
                "failed": (
                    latest_watch["warnings"][0].get("failed", 0)
                    if latest_watch and latest_watch["warnings"]
                    else (0 if latest_watch else None)
                ),
                "skipped": (
                    latest_watch["warnings"][0].get("skipped", 0)
                    if latest_watch and latest_watch["warnings"]
                    else 0
                ),
                "pending_due": pending_due,
                "due_count": watch_queue["due_count"],
                "watched_sources": watch_queue["watched_sources"],
                "failed_sources": watch_queue["failed_sources"],
                "next_due_at": watch_queue["next_due_at"],
            },
        }

    async def inspect_source(self, request: InspectRequest) -> dict[str, Any]:
        return await self.source_inspector.inspect(request.target)

    async def save_item(self, request: SaveRequest) -> dict[str, Any]:
        now = self._now()
        if request.item_id is not None:
            item = self.repository.get_item(request.item_id)
            if item is None:
                raise NotFoundError(f"Item {request.item_id} was not found.")
            changes = {
                key: value
                for key, value in {
                    "triage_status": request.triage_status,
                    "category": request.category.strip() if request.category else None,
                    "is_watched": request.is_watched,
                    "notes": request.notes,
                }.items()
                if value is not None
            }
            if all(item.get(key) == value for key, value in changes.items()):
                return {
                    "item_id": request.item_id,
                    "revision": item["revision"],
                    "triage_status": item["triage_status"],
                    "action": "unchanged",
                }
            result = self.repository.update_item(
                request.item_id,
                request.expected_revision,
                changes,
                now=now,
            )
            if result is None:
                current = self.repository.get_item(request.item_id)
                if current is None:
                    raise NotFoundError(f"Item {request.item_id} was not found.")
                raise RevisionConflictError(request.item_id, current["revision"])
            item = self.repository.get_item(request.item_id)
            return {
                "item_id": request.item_id,
                "revision": result["revision"],
                "triage_status": item["triage_status"],
                "action": "updated",
            }

        source_target = request.source["target"]
        inspection = await self.inspect_source(InspectRequest(target=source_target))
        candidate = self._candidate_from_inspection(inspection)
        existing = self.repository.get_source(candidate.source_type, candidate.source_uid)
        if existing:
            item = self.repository.get_item(existing["item_id"])
            if item is None:
                raise NotFoundError("The source points to a missing item.")
            requested = self._requested_source_fields(request)
            if requested:
                raise RevisionConflictError(item["item_id"], item["revision"])
            return {
                "item_id": item["item_id"],
                "revision": item["revision"],
                "triage_status": item["triage_status"],
                "action": "unchanged",
            }
        same_paper_type = (
            "hf_paper" if candidate.source_type == "arxiv" else
            "arxiv" if candidate.source_type == "hf_paper" else None
        )
        related = (
            self.repository.get_source(same_paper_type, candidate.source_uid)
            if same_paper_type
            else None
        )
        requested = self._requested_source_fields(request)
        if related and requested:
            item = self.repository.get_item(related["item_id"])
            if item:
                raise RevisionConflictError(item["item_id"], item["revision"])

        with self.repository.transaction() as connection:
            ingested = self.resolver.ingest_candidate(
                candidate,
                conn=connection,
                now=now,
                triage_status=request.triage_status or "saved",
                category=request.category.strip() if request.category else "general",
                is_watched=bool(request.is_watched),
                notes=request.notes or "",
                run_id=None,
            )
        if ingested.item_id is None:
            pending = self.repository.get_pending_source(ingested.pending_id)
            raise InfoDeskError(
                "MERGE_CONFLICT",
                "The source matched multiple existing items and needs review.",
                details={
                    "pending_source_id": ingested.pending_id,
                    "candidate_item_ids": pending["matched_item_ids"] if pending else [],
                },
            )
        item = self.repository.get_item(ingested.item_id)
        return {
            "item_id": ingested.item_id,
            "revision": item["revision"],
            "triage_status": item["triage_status"],
            "action": "created" if ingested.item_created else "updated",
        }

    async def fetch_updates(self, request: FetchRequest) -> dict[str, Any]:
        return await self.sync_engine.run(request, scheduled=False)

    async def sync_scheduled(self) -> dict[str, Any]:
        request = FetchRequest(
            days=self.settings.initial_lookback_days,
            feeds=self.settings.enabled_feeds,
            topics=self.settings.topics,
        )
        return await self.sync_engine.run(request, scheduled=True)

    def _candidate_from_inspection(self, data: dict[str, Any]) -> CandidateSource:
        source = data["source"]
        feed = self.adapters.feed_for_source_type(source["source_type"])
        capabilities = self.adapters.capabilities(feed) if feed else None
        metadata = dict(source.get("metadata", {}))
        if source["source_type"] == "github_repo":
            metadata["code_status"] = data.get("code_status", "unknown")
            metadata["code_signals"] = data.get("signals", [])
        return CandidateSource(
            source_type=source["source_type"],
            feed=feed,
            watch_interval_hours=(capabilities.watch_interval_hours if capabilities else None),
            source_uid=source["source_uid"],
            external_id=source["external_id"],
            url=source["url"],
            title=source["title"],
            description=source.get("description", ""),
            published_at=source.get("published_at"),
            source_updated_at=source.get("source_updated_at"),
            metadata=metadata,
            extracted_links=data.get("extracted_links", []),
        )

    @staticmethod
    def _requested_source_fields(request: SaveRequest) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "triage_status": request.triage_status,
                "category": request.category,
                "is_watched": request.is_watched,
                "notes": request.notes,
            }.items()
            if value is not None
        }

    @staticmethod
    def _item_view(item: dict[str, Any], *, include_notes: bool = False) -> ItemView:
        return ItemView.model_validate(
            {
                **item,
                "sources": item.get("sources", []),
                "sources_truncated": item.get("sources_truncated", False),
                "notes": item.get("notes") if include_notes else None,
            }
        )

    @staticmethod
    def _encode_cursor(
        filters: dict[str, Any], before: tuple[str, int]
    ) -> str:
        digest = hashlib.sha256(
            json.dumps(filters, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
        payload = {"version": 1, "filter": digest, "before": before}
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(encoded).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str, filters: dict[str, Any]) -> tuple[str, int]:
        try:
            padding = "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
            expected_filter = hashlib.sha256(
                json.dumps(filters, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:16]
            if payload.get("version") != 1 or payload.get("filter") != expected_filter:
                raise ValueError
            before = payload["before"]
            return str(before[0]), int(before[1])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise InfoDeskError("INVALID_CURSOR", "The search cursor is invalid or expired.") from error

    @staticmethod
    def _utc_string(value: datetime | None) -> str | None:
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") if value else None

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _upstream_error(response: httpx.Response) -> InfoDeskError:
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            try:
                seconds = float(retry_after) if retry_after else None
            except ValueError:
                seconds = None
            return UpstreamError(
                "RATE_LIMITED",
                "The source rate limit was reached.",
                retry_after_seconds=seconds,
            )
        if response.status_code in {401, 403}:
            return InfoDeskError("UPSTREAM_FORBIDDEN", "The source denied access.")
        if response.status_code == 404:
            return NotFoundError("The source was not found.")
        if response.status_code >= 500:
            return UpstreamError("UPSTREAM_UNAVAILABLE", "The source is temporarily unavailable.")
        return InfoDeskError("UPSTREAM_UNAVAILABLE", "The source returned an unsuccessful response.")
