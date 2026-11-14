"""Windowed discovery, checkpoints, rate limits, and feed locks."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from info_desk.config import Settings
from info_desk.contracts import FetchRequest
from info_desk.exceptions import InfoDeskError, ValidationError
from info_desk.locking import LockConflictError, LockOperationError, acquire_file_lock
from info_desk.resolver import IdentityResolver
from info_desk.sources.base import AdapterError
from info_desk.sources.registry import AdapterRegistry
from info_desk.storage.repository import Repository
from info_desk.watch_refresh import WatchRefresher

ARXIV_CATEGORIES = ["cs.AI", "cs.CL", "cs.LG", "cs.CV", "cs.RO"]
logger = logging.getLogger(__name__)


def _lock_error(error: BaseException, *, phase: str) -> InfoDeskError | None:
    if phase == "release":
        return InfoDeskError(
            "SYNC_LOCK_RELEASE_FAILED",
            "Synchronization work completed, but a feed lock could not be released.",
            retryable=True,
            details={"error_type": type(error.__cause__ or error).__name__},
        )
    if isinstance(error, LockConflictError):
        return InfoDeskError(
            "SYNC_ALREADY_RUNNING",
            "A synchronization for one of the selected feeds is already running.",
            retryable=True,
        )
    if isinstance(error, PermissionError):
        return InfoDeskError(
            "SYNC_LOCK_PERMISSION",
            "Permission denied while opening a synchronization lock.",
            details={"error_type": type(error).__name__},
        )
    if isinstance(error, OSError):
        return InfoDeskError(
            "SYNC_LOCK_IO",
            "Filesystem error while opening a synchronization lock.",
            details={"error_type": type(error).__name__},
        )
    if isinstance(error, LockOperationError):
        return InfoDeskError(
            "SYNC_LOCK_ERROR",
            "The synchronization lock backend failed.",
            details={"error_type": type(error.__cause__).__name__ if error.__cause__ else type(error).__name__},
        )
    return None


class SyncEngine:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        adapters: dict[str, Any],
        resolver: IdentityResolver,
        watch_refresher: WatchRefresher,
        upstream_error: Any,
        *,
        monotonic: Any = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.registry = adapters if isinstance(adapters, AdapterRegistry) else AdapterRegistry.from_mapping(adapters)
        self.adapters = self.registry
        self.resolver = resolver
        self.watch_refresher = watch_refresher
        self.upstream_error = upstream_error
        self.monotonic = monotonic or time.monotonic

    async def run(self, request: FetchRequest, *, scheduled: bool = False) -> dict[str, Any]:
        return await self._run_sync(request, scheduled=scheduled)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    async def _run_sync(
        self, request: FetchRequest, *, scheduled: bool
    ) -> dict[str, Any]:
        feeds = request.feeds or self.settings.enabled_feeds
        if not feeds:
            raise ValidationError("At least one feed must be enabled.")
        unknown = sorted(set(feeds) - set(self.registry))
        if unknown:
            raise ValidationError(f"Unsupported feeds: {', '.join(unknown)}")
        disabled = sorted(set(feeds) - set(self.settings.enabled_feeds))
        if disabled:
            raise ValidationError(f"Feeds are disabled by configuration: {', '.join(disabled)}")

        lock_stack = contextlib.ExitStack()
        lock_feeds = set(feeds) | (set(self.registry) if scheduled else set())
        try:
            for feed in sorted(lock_feeds):
                lock_stack.enter_context(self._feed_lock(feed))
        except BaseException as error:
            try:
                lock_stack.close()
            except BaseException:
                logger.exception("Failed to release partially acquired synchronization locks")
            mapped = _lock_error(error, phase="acquisition")
            if mapped is not None:
                raise mapped from error
            raise
        budget = (
            self.settings.scheduled_budget_sec
            if scheduled
            else self.settings.refresh_budget_sec
        )
        root_run_id: int | None = None
        try:
            started_at = self._now()
            self.repository.recover_stale_runs(lock_feeds, now=started_at)
            root_run_id = self.repository.start_run(
                started_at=started_at,
                job_key=f"root:{','.join(sorted(set(feeds)))}",
            )
            end = datetime.now(UTC).replace(microsecond=0)
            default_start = end - timedelta(days=request.days)
            topics = request.topics or self.settings.topics
            pending_jobs = [
                (feed, query)
                for feed in feeds
                for query in self._queries_for_feed(feed, topics)
            ]
            jobs = []
            total_stats = {
                "fetched": 0,
                "new_items": 0,
                "new_sources": 0,
                "new_events": 0,
                "conflicts": 0,
            }
            deadline = self.monotonic() + budget
            watch_reserve = min(budget, max(0.25, budget * 0.2)) if scheduled else 0.0
            discovery_deadline = deadline - watch_reserve
            watch_refresh = None

            for index, (feed, query) in enumerate(pending_jobs):
                stable_job_key = self._job_key(feed, query, None)
                manual_job_key = self._job_key(feed, query, "manual")
                previous_state = (
                    self.repository.get_sync_state(stable_job_key)
                    if scheduled
                    else None
                )
                if scheduled and previous_state:
                    if previous_state["completed_until"]:
                        previous_end = datetime.fromisoformat(
                            previous_state["completed_until"]
                        )
                        window_start = previous_end - timedelta(
                            hours=self.settings.overlap_hours
                        )
                    elif previous_state["coverage_from"]:
                        window_start = datetime.fromisoformat(
                            previous_state["coverage_from"]
                        )
                    else:
                        window_start = default_start
                else:
                    window_start = default_start
                if self.registry.capabilities(feed).discovery_mode == "none":
                    job_key = stable_job_key if scheduled else manual_job_key
                    self.repository.upsert_sync_state(
                        job_key, feed, query, now=self._now(),
                        last_status="skipped", last_error="DISCOVERY_UNSUPPORTED",
                    )
                    jobs.append(
                        self._skipped_job(
                            feed, query, (window_start, end), "DISCOVERY_UNSUPPORTED",
                            job_key=job_key,
                        )
                    )
                    continue
                now_monotonic = self.monotonic()
                if now_monotonic >= discovery_deadline:
                    job_key = stable_job_key if scheduled else manual_job_key
                    self.repository.upsert_sync_state(
                        job_key, feed, query, now=self._now(),
                        last_status="skipped", last_error="BUDGET_EXHAUSTED",
                    )
                    jobs.append(
                        self._skipped_job(
                            feed, query, (window_start, end), "BUDGET_EXHAUSTED",
                            job_key=job_key,
                        )
                    )
                    continue
                jobs_left = len(pending_jobs) - index
                job_deadline = min(
                    discovery_deadline,
                    now_monotonic + (discovery_deadline - now_monotonic) / jobs_left,
                )
                job = await self._run_segmented_job(
                    feed,
                    query,
                    (window_start, end),
                    root_run_id=root_run_id,
                    deadline=job_deadline,
                    # Manual work has its own stable namespace: retries resume, but manual
                    # backfills cannot advance the scheduled feed's watermark.
                    job_key=stable_job_key if scheduled else manual_job_key,
                )
                jobs.append(job)
                for key in total_stats:
                    total_stats[key] += job["stats"][key]

            if scheduled:
                watch_refresh = await self.watch_refresher.refresh_watched(
                    root_run_id=root_run_id, deadline=deadline
                )
                total_stats["new_items"] += watch_refresh["new_items"]
                total_stats["new_sources"] += watch_refresh["new_sources"]
                total_stats["new_events"] += watch_refresh["new_events"]

            statuses = {job["status"] for job in jobs}
            watch_status = watch_refresh["last_status"] if watch_refresh else None
            if (
                statuses
                and statuses == {"success"}
                and (
                    watch_refresh is None
                    or (
                        watch_refresh["last_status"] == "success"
                        and not watch_refresh["pending_due"]
                    )
                )
            ):
                status = "success"
            elif statuses and statuses == {"skipped"} and watch_status in {
                None, "success", "skipped"
            }:
                status = "skipped"
            elif any(job["status"] in {"partial", "success"} for job in jobs) or (
                watch_refresh and watch_refresh["checked"]
            ):
                status = "partial"
            else:
                status = "failed"
            preview = self.repository.list_events_for_run(root_run_id, limit=request.limit)
            finished_at = self._now()
            warnings = [
                {
                    "code": job["error_code"],
                    "message": f"{job['feed']} scan incomplete.",
                    "feed": job["feed"],
                    "retry_after_seconds": job.get("retry_after_seconds"),
                }
                for job in jobs
                if job["error_code"]
            ]
            if watch_refresh and watch_refresh["last_status"] != "success":
                warnings.append(
                    {
                        "code": "WATCH_REFRESH_PARTIAL",
                        "message": "Some watched sources remain due for refresh.",
                        "feed": None,
                    }
                )
            with self.repository.transaction() as connection:
                self.repository.update_run(
                    root_run_id,
                    ended_at=finished_at,
                    status=status,
                    fetched_count=total_stats["fetched"],
                    new_sources=total_stats["new_sources"],
                    new_items=total_stats["new_items"],
                    new_events=total_stats["new_events"],
                    conflict_count=total_stats["conflicts"],
                    warnings=warnings,
                    error_code=(
                        None if status == "success"
                        else "BUDGET_EXHAUSTED" if status == "skipped"
                        else "SYNC_PARTIAL"
                    ),
                    conn=connection,
                )
            result = {
                "run_id": root_run_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "status": status,
                "jobs": jobs,
                "stats": total_stats,
                "events_preview": preview["events"],
                "events_preview_truncated": preview["has_more"],
                "watch_refresh": watch_refresh,
                "warnings": warnings,
            }
        except BaseException as error:
            if isinstance(error, Exception):
                if root_run_id is not None:
                    try:
                        self.repository.update_run(
                            root_run_id,
                            ended_at=self._now(),
                            status="failed",
                            error_code="INTERNAL_ERROR",
                        )
                    except Exception:
                        logger.exception("Failed to mark the synchronization run as failed")
                logger.exception("Synchronization orchestration failed")
            try:
                lock_stack.close()
            except BaseException:
                logger.exception("Failed to release synchronization locks after primary failure")
            raise
        else:
            try:
                lock_stack.close()
            except Exception as error:
                mapped = _lock_error(error, phase="release")
                if mapped is None:
                    raise
                logger.exception("Synchronization locks could not be released after success")
                raise mapped from error
            return result

    async def _run_discovery_job(
        self,
        feed: str,
        query: dict[str, Any],
        window: tuple[datetime, datetime],
        *,
        root_run_id: int,
        deadline: float,
        job_key: str,
        split_depth: int = 0,
        page_token: str | None = None,
        persist_state: bool = True,
    ) -> dict[str, Any]:
        adapter = self.registry[feed]
        start, end = window
        now = self._now()
        with self.repository.transaction() as connection:
            child_run_id = self.repository.start_run(
                started_at=now,
                root_run_id=root_run_id,
                job_key=job_key,
                feed=feed,
                window_start=self._iso(start),
                window_end=self._iso(end),
                conn=connection,
            )
        stats = {"fetched": 0, "new_items": 0, "new_sources": 0, "new_events": 0, "conflicts": 0}
        initial_page_token = page_token
        pages = 0
        error_code = None
        retry_after_seconds = None
        complete = False
        resume_safe = False
        checkpoint_token = None
        while pages < self.settings.max_pages_per_window and self.monotonic() < deadline:
            pages += 1
            try:
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    error_code = "UPSTREAM_TIMEOUT"
                    break
                page = await asyncio.wait_for(
                    adapter.discover(query, window, page_token),
                    timeout=remaining,
                )
            except (AdapterError, InfoDeskError) as error:
                error_code = getattr(error, "code", "UPSTREAM_UNAVAILABLE")
                retry_after_seconds = getattr(error, "retry_after_seconds", None)
                break
            except TimeoutError:
                error_code = "UPSTREAM_TIMEOUT"
                break
            except httpx.TimeoutException:
                error_code = "UPSTREAM_TIMEOUT"
                break
            except httpx.HTTPStatusError as error:
                upstream = self.upstream_error(error.response)
                error_code = upstream.code
                retry_after_seconds = upstream.retry_after_seconds
                break
            except httpx.HTTPError:
                error_code = "UPSTREAM_UNAVAILABLE"
                break
            except Exception:
                logger.exception("Unexpected adapter failure for feed %s", feed)
                error_code = "INTERNAL_ERROR"
                break

            page_stats = {
                "fetched": len(page.records),
                "new_items": 0,
                "new_sources": 0,
                "new_events": 0,
                "conflicts": 0,
            }
            try:
                with self.repository.transaction() as connection:
                    for candidate in page.records:
                        if candidate.source_type not in self.registry.capabilities(
                            feed
                        ).source_types:
                            raise AdapterError(
                                "ADAPTER_CONTRACT_VIOLATION",
                                "Adapter returned an undeclared source type.",
                            )
                        candidate.feed = feed
                        candidate.watch_interval_hours = self.registry.capabilities(
                            feed
                        ).watch_interval_hours
                        ingested = self.resolver.ingest_candidate(
                            candidate,
                            conn=connection,
                            now=self._now(),
                            run_id=child_run_id,
                        )
                        page_stats["new_sources"] += int(ingested.source_created)
                        page_stats["new_items"] += int(ingested.item_created)
                        page_stats["new_events"] += ingested.event_count
                        page_stats["conflicts"] += ingested.conflict_count
            except AdapterError as error:
                logger.error("Adapter contract failed for feed %s: %s", feed, error.code)
                error_code = error.code
                break
            except Exception:
                logger.exception("Failed to commit a page for feed %s", feed)
                error_code = "INTERNAL_ERROR"
                break
            for key in stats:
                stats[key] += page_stats[key]

            if page.source_response_incomplete:
                error_code = page.warnings[0] if page.warnings else "PARTIAL_SYNC"
                break
            if page.next_page_token is None:
                complete = page.page_complete
                if not complete:
                    error_code = "PARTIAL_SYNC"
                elif (
                    self.registry.capabilities(feed).discovery_mode == "cursor"
                    and self.registry.capabilities(feed).durable_cursor
                ):
                    checkpoint_token = page.checkpoint_token
                    if checkpoint_token is None:
                        complete = False
                        error_code = "CURSOR_CHECKPOINT_MISSING"
                break
            page_token = page.next_page_token
            resume_safe = page.resume_safe

        if not complete and error_code is None:
            if self.monotonic() >= deadline:
                error_code = "UPSTREAM_TIMEOUT"
            elif pages >= self.settings.max_pages_per_window and page_token is not None:
                error_code = "PAGE_LIMIT"
            else:
                error_code = "PARTIAL_SYNC"

        known_incomplete = {
            "GITHUB_INCOMPLETE_RESULTS",
            "GITHUB_SEARCH_LIMIT",
            "PARTIAL_SYNC",
            "PAGE_LIMIT",
            "CURSOR_CHECKPOINT_MISSING",
        }
        status = "success" if complete and error_code is None else (
            "partial"
            if stats["fetched"] or pages > 1 or error_code in known_incomplete
            else "failed"
        )
        if status != "success" and error_code is None:
            error_code = "PARTIAL_SYNC"
        completed_until = (
            self._iso(end)
            if status == "success"
            and self.registry.capabilities(feed).supports_time_coverage
            else None
        )
        finished = self._now()
        with self.repository.transaction() as connection:
            self.repository.update_run(
                child_run_id,
                ended_at=finished,
                status=status,
                completed_until=completed_until,
                fetched_count=stats["fetched"],
                new_sources=stats["new_sources"],
                new_items=stats["new_items"],
                new_events=stats["new_events"],
                conflict_count=stats["conflicts"],
                error_code=error_code,
                conn=connection,
            )
            if persist_state:
                self.repository.upsert_sync_state(
                job_key,
                feed,
                query,
                now=finished,
                    coverage_from=(
                        self._iso(start)
                        if self.registry.capabilities(feed).supports_time_coverage
                        else None
                    ),
                completed_until=completed_until,
                last_status=status,
                last_error=error_code or "",
                    conn=connection,
                )
        return {
            "job_key": job_key,
            "feed": feed,
            "window_start": self._iso(start),
            "window_end": self._iso(end),
            "completed_until": completed_until,
            "status": status,
            "error_code": error_code,
            "retry_after_seconds": retry_after_seconds,
            "next_page_token": page_token,
            "resume_safe": resume_safe or (initial_page_token is not None and page_token == initial_page_token),
            "checkpoint_token": checkpoint_token,
            "stats": stats,
        }

    async def _run_segmented_job(
        self,
        feed: str,
        query: dict[str, Any],
        requested_window: tuple[datetime, datetime],
        *,
        root_run_id: int,
        deadline: float,
        job_key: str,
    ) -> dict[str, Any]:
        state = self.repository.get_sync_state(job_key)
        segment_records = self.repository.list_sync_segment_records(job_key)
        segments = self.repository.list_sync_segments(job_key)
        capabilities = self.registry.capabilities(feed)
        if (
            segment_records
            and state
            and state.get("resume_window_start")
            and state.get("resume_window_end")
        ):
            active_window = (
                datetime.fromisoformat(state["resume_window_start"]),
                datetime.fromisoformat(state["resume_window_end"]),
            )
        else:
            active_window = requested_window
        start_text, end_text = map(self._iso, active_window)
        if not segment_records:
            now = self._now()
            with self.repository.transaction() as connection:
                self.repository.upsert_sync_state(
                    job_key, feed, query, now=now,
                    coverage_from=(start_text if capabilities.supports_time_coverage else None),
                    last_error="", conn=connection,
                )
                self.repository.set_sync_resume(
                    job_key, token=None, window=(start_text, end_text), conn=connection
                )
                initial_token = (
                    state.get("provider_cursor")
                    if state and capabilities.discovery_mode == "cursor"
                    else None
                )
                self.repository.enqueue_sync_segment(
                    job_key, start_text, end_text, page_token=initial_token, conn=connection
                )
            segments = self.repository.list_sync_segments(job_key)

        stats = {"fetched": 0, "new_items": 0, "new_sources": 0, "new_events": 0, "conflicts": 0}
        attempted: set[int] = set()
        successful_segments = 0
        processed_segments = 0
        last_error = None
        last_retry = None
        split_budget = self.settings.sync_max_window_splits
        while segments:
            segment = next((entry for entry in segments if entry["id"] not in attempted), None)
            if (
                segment is None
                or self.monotonic() >= deadline
                or processed_segments >= self.settings.sync_max_segments_per_run
            ):
                break
            attempted.add(int(segment["id"]))
            processed_segments += 1
            segment_window = (
                datetime.fromisoformat(segment["window_start"]),
                datetime.fromisoformat(segment["window_end"]),
            )
            result = await self._run_discovery_job(
                feed, query, segment_window, root_run_id=root_run_id, deadline=deadline,
                job_key=job_key, page_token=segment["page_token"], persist_state=False,
            )
            for key in stats:
                stats[key] += result["stats"][key]
            if result["status"] == "success":
                cursor_checkpoint = (
                    result.get("checkpoint_token")
                    if capabilities.discovery_mode == "cursor" and capabilities.durable_cursor
                    else None
                )
                if cursor_checkpoint is not None:
                    self.repository.complete_sync_segment(
                        int(segment["id"]), provider_cursor=cursor_checkpoint
                    )
                else:
                    self.repository.complete_sync_segment(int(segment["id"]))
                successful_segments += 1
            else:
                code = result["error_code"] or "PARTIAL_SYNC"
                last_error = code
                last_retry = result.get("retry_after_seconds")
                caps = self.registry.capabilities(feed)
                split_requested = code == "PAGE_LIMIT" or code in {
                    "GITHUB_INCOMPLETE_RESULTS", "GITHUB_SEARCH_LIMIT", "PARTIAL_SYNC"
                }
                split = None
                if (
                    split_requested
                    and int(segment["split_depth"]) < split_budget
                    and caps.window_filtering == "server"
                ):
                    split = self._split_window(
                        segment_window, caps.window_resolution_seconds
                    )
                if split:
                    self.repository.split_sync_segment(
                        int(segment["id"]),
                        tuple(map(self._iso, split[0])),
                        tuple(map(self._iso, split[1])),
                    )
                elif caps.durable_cursor and result.get("resume_safe") and result.get("next_page_token"):
                    self.repository.update_sync_segment_failure(
                        int(segment["id"]), now=self._now(), error_code=code,
                        page_token=result["next_page_token"],
                    )
                    # Keep a durable cursor for the next run. Reopening it in this same run
                    # would turn the per-window page cap into an accidental unlimited loop.
                else:
                    diagnostic = code
                    if split_requested and split is None:
                        diagnostic = (
                            "API_COMPLETENESS_UNVERIFIED"
                            if caps.window_resolution_seconds is None
                            or caps.window_filtering != "server"
                            else "WINDOW_SPLIT_LIMIT"
                        )
                    self.repository.update_sync_segment_failure(
                        int(segment["id"]), now=self._now(), error_code=diagnostic,
                        page_token=(result["next_page_token"]
                                    if caps.durable_cursor and result.get("resume_safe") else None),
                    )
                    last_error = diagnostic
            segments = self.repository.list_sync_segments(job_key)

        if not segments:
            status = "success"
            error = None
            completed_until = end_text if capabilities.supports_time_coverage else None
        else:
            incomplete_codes = {
                "API_COMPLETENESS_UNVERIFIED",
                "CURSOR_CHECKPOINT_MISSING",
                "GITHUB_INCOMPLETE_RESULTS",
                "GITHUB_SEARCH_LIMIT",
                "PAGE_LIMIT",
                "PARTIAL_SYNC",
                "WINDOW_SPLIT_LIMIT",
            }
            status = (
                "partial"
                if successful_segments or stats["fetched"] or last_error in incomplete_codes
                else "skipped" if last_error is None else "failed"
            )
            error = last_error or (
                "SEGMENT_BUDGET_EXHAUSTED"
                if processed_segments >= self.settings.sync_max_segments_per_run
                else "BUDGET_EXHAUSTED"
            )
            completed_until = self.repository.contiguous_sync_coverage(
                job_key, start_text, end_text
            )
        now = self._now()
        with self.repository.transaction() as connection:
            self.repository.upsert_sync_state(
                job_key, feed, query, now=now,
                coverage_from=(start_text if capabilities.supports_time_coverage else None),
                completed_until=completed_until, last_status=status,
                last_error=error or "", conn=connection,
            )
            self.repository.set_sync_resume(
                job_key,
                token=(segments[0]["page_token"] if segments else None),
                window=(start_text, end_text) if segments else None,
                conn=connection,
            )
            if not segments:
                self.repository.delete_sync_segments(job_key, conn=connection)
        return {
            "job_key": job_key, "feed": feed,
            "window_start": start_text, "window_end": end_text,
            "completed_until": completed_until, "status": status,
            "error_code": error, "retry_after_seconds": last_retry,
            "stats": stats,
        }

    def _feed_lock(self, feed: str):
        lock_path = self.settings.home / "run" / f"{feed}.lock"
        return acquire_file_lock(lock_path)

    def _queries_for_feed(self, feed: str, topics: list[str]) -> list[dict[str, Any]]:
        builder = self.registry.capabilities(feed).query_builder
        return builder(topics) if builder else [{}]

    @staticmethod
    def _split_window(
        window: tuple[datetime, datetime],
        minimum_resolution_seconds: int | None,
    ) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]] | None:
        start, end = window
        duration = (end - start).total_seconds()
        if minimum_resolution_seconds is None or duration <= minimum_resolution_seconds:
            return None
        midpoint = (start + (end - start) / 2).timestamp()
        resolution = max(1, minimum_resolution_seconds)
        split_epoch = int(midpoint // resolution) * resolution
        start_epoch = start.timestamp()
        if split_epoch <= start_epoch:
            split_epoch = (int(start_epoch // resolution) + 1) * resolution
        split = datetime.fromtimestamp(split_epoch, UTC)
        if split <= start or split >= end:
            return None
        return (start, split), (split, end)

    @staticmethod
    def _split_github_window(
        window: tuple[datetime, datetime],
    ) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]] | None:
        return SyncEngine._split_window(window, 1)

    @staticmethod
    def _skipped_job(
        feed: str,
        query: dict[str, Any],
        window: tuple[datetime, datetime],
        error_code: str,
        *,
        job_key: str,
    ) -> dict[str, Any]:
        return {
            "job_key": job_key,
            "feed": feed,
            "window_start": SyncEngine._iso(window[0]),
            "window_end": SyncEngine._iso(window[1]),
            "completed_until": None,
            "status": "skipped",
            "error_code": error_code,
            "query": query,
            "stats": {"fetched": 0, "new_items": 0, "new_sources": 0, "new_events": 0, "conflicts": 0},
        }

    @staticmethod
    def _job_key(
        feed: str, query: dict[str, Any], root_run_id: int | str | None
    ) -> str:
        canonical = json.dumps(
            {"feed": feed, "query": query, "root_run_id": root_run_id},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()
