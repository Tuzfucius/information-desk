"""Windowed discovery, checkpoints, rate limits, and feed locks."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import httpx

from info_desk.config import Settings
from info_desk.contracts import FetchRequest
from info_desk.exceptions import InfoDeskError, ValidationError
from info_desk.resolver import IdentityResolver
from info_desk.sources.base import AdapterError
from info_desk.storage.repository import Repository
from info_desk.watch_refresh import WatchRefresher

FEEDS = ("arxiv", "hf_papers", "hf_models", "github")
ARXIV_CATEGORIES = ["cs.AI", "cs.CL", "cs.LG", "cs.CV", "cs.RO"]
logger = logging.getLogger(__name__)


class SyncEngine:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        adapters: dict[str, Any],
        resolver: IdentityResolver,
        watch_refresher: WatchRefresher,
        upstream_error: Any,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.adapters = adapters
        self.resolver = resolver
        self.watch_refresher = watch_refresher
        self.upstream_error = upstream_error

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
        unknown = sorted(set(feeds) - set(FEEDS))
        if unknown:
            raise ValidationError(f"Unsupported feeds: {', '.join(unknown)}")
        disabled = sorted(set(feeds) - set(self.settings.enabled_feeds))
        if disabled:
            raise ValidationError(f"Feeds are disabled by configuration: {', '.join(disabled)}")

        lock_stack = contextlib.ExitStack()
        try:
            for feed in sorted(set(feeds)):
                lock_stack.enter_context(self._feed_lock(feed))
        except BlockingIOError as error:
            lock_stack.close()
            raise InfoDeskError(
                "SYNC_ALREADY_RUNNING",
                "A synchronization for one of the selected feeds is already running.",
                retryable=True,
            ) from error
        budget = (
            self.settings.scheduled_budget_sec
            if scheduled
            else self.settings.refresh_budget_sec
        )
        root_run_id: int | None = None
        try:
            started_at = self._now()
            self.repository.recover_stale_runs(feeds, now=started_at)
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
            deadline = time.monotonic() + budget
            watch_reserve = min(budget, max(0.25, budget * 0.2)) if scheduled else 0.0
            discovery_deadline = deadline - watch_reserve
            watch_refresh = None

            for index, (feed, query) in enumerate(pending_jobs):
                stable_job_key = self._job_key(feed, query, None)
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
                now_monotonic = time.monotonic()
                if now_monotonic >= discovery_deadline:
                    jobs.append(
                        self._skipped_job(feed, query, window_start, end, "UPSTREAM_TIMEOUT")
                    )
                    continue
                jobs_left = len(pending_jobs) - index
                job_deadline = min(
                    discovery_deadline,
                    now_monotonic + (discovery_deadline - now_monotonic) / jobs_left,
                )
                job = await self._run_discovery_job(
                    feed,
                    query,
                    (window_start, end),
                    root_run_id=root_run_id,
                    deadline=job_deadline,
                    job_key=stable_job_key if scheduled else self._job_key(feed, query, root_run_id),
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
            elif any(job["stats"]["fetched"] or job["status"] == "success" for job in jobs) or (
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
                    error_code=None if status == "success" else "SYNC_PARTIAL",
                    conn=connection,
                )
            return {
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
        except Exception:
            if root_run_id is not None:
                self.repository.update_run(
                    root_run_id,
                    ended_at=self._now(),
                    status="failed",
                    error_code="INTERNAL_ERROR",
                )
            logger.exception("Synchronization orchestration failed")
            raise
        finally:
            lock_stack.close()

    async def _run_discovery_job(
        self,
        feed: str,
        query: dict[str, Any],
        window: tuple[datetime, datetime],
        *,
        root_run_id: int,
        deadline: float,
        job_key: str,
    ) -> dict[str, Any]:
        adapter = self.adapters[feed]
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
        page_token = None
        pages = 0
        error_code = None
        retry_after_seconds = None
        complete = False
        while pages < self.settings.max_pages_per_window and time.monotonic() < deadline:
            pages += 1
            try:
                remaining = max(0.1, deadline - time.monotonic())
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
                break
            page_token = page.next_page_token

        if not complete and error_code is None:
            if time.monotonic() >= deadline:
                error_code = "UPSTREAM_TIMEOUT"
            elif pages >= self.settings.max_pages_per_window and page_token is not None:
                error_code = "PAGE_LIMIT"
            else:
                error_code = "PARTIAL_SYNC"

        if (
            feed == "github"
            and error_code in {"GITHUB_INCOMPLETE_RESULTS", "GITHUB_SEARCH_LIMIT"}
        ):
            segments = self._split_github_window(window)
            if segments:
                left, right = segments
                left_result = await self._run_discovery_job(
                    feed,
                    query,
                    left,
                    root_run_id=root_run_id,
                    deadline=deadline,
                    job_key=self._job_key(
                        feed,
                        {**query, "window_start": self._iso(left[0]), "window_end": self._iso(left[1])},
                        root_run_id,
                    ),
                )
                right_result = await self._run_discovery_job(
                    feed,
                    query,
                    right,
                    root_run_id=root_run_id,
                    deadline=deadline,
                    job_key=self._job_key(
                        feed,
                        {**query, "window_start": self._iso(right[0]), "window_end": self._iso(right[1])},
                        root_run_id,
                    ),
                )
                for key in stats:
                    stats[key] += left_result["stats"][key] + right_result["stats"][key]
                complete = (
                    left_result["status"] == "success"
                    and right_result["status"] == "success"
                )
                error_code = None if complete else "PARTIAL_SYNC"

        known_incomplete = {
            "GITHUB_INCOMPLETE_RESULTS",
            "GITHUB_SEARCH_LIMIT",
            "PARTIAL_SYNC",
            "PAGE_LIMIT",
        }
        status = "success" if complete and error_code is None else (
            "partial"
            if stats["fetched"] or pages > 1 or error_code in known_incomplete
            else "failed"
        )
        if status != "success" and error_code is None:
            error_code = "PARTIAL_SYNC"
        completed_until = self._iso(end) if status == "success" else None
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
            self.repository.upsert_sync_state(
                job_key,
                feed,
                query,
                now=finished,
                coverage_from=self._iso(start),
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
            "stats": stats,
        }

    def _feed_lock(self, feed: str):
        lock_path = self.settings.home / "run" / f"{feed}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(lock_path.parent, 0o700)
        handle = lock_path.open("a+")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        return _Lock(handle)

    @staticmethod
    def _queries_for_feed(feed: str, topics: list[str]) -> list[dict[str, Any]]:
        if feed == "arxiv":
            return [{"topics": topics, "categories": ARXIV_CATEGORIES}]
        if feed == "hf_papers":
            return [{}]
        if feed == "hf_models":
            return [{"topic": topic} for topic in topics]
        return [
            {"topic": topic, "mode": mode}
            for topic in topics
            for mode in ("created", "updated")
        ]

    @staticmethod
    def _split_github_window(
        window: tuple[datetime, datetime],
    ) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]] | None:
        start, end = window
        duration = (end - start).total_seconds()
        if duration <= 1:
            return None
        split = (start + (end - start) / 2).replace(microsecond=0)
        if split <= start or split >= end:
            return None
        return (start, split), (split, end)

    @staticmethod
    def _skipped_job(
        feed: str,
        query: dict[str, Any],
        window: tuple[datetime, datetime],
        error_code: str,
    ) -> dict[str, Any]:
        return {
            "job_key": "",
            "feed": feed,
            "window_start": SyncEngine._iso(window[0]),
            "window_end": SyncEngine._iso(window[1]),
            "completed_until": None,
            "status": "failed",
            "error_code": error_code,
            "query": query,
            "stats": {"fetched": 0, "new_items": 0, "new_sources": 0, "new_events": 0, "conflicts": 0},
        }

    @staticmethod
    def _job_key(
        feed: str, query: dict[str, Any], root_run_id: int | None
    ) -> str:
        canonical = json.dumps(
            {"feed": feed, "query": query, "root_run_id": root_run_id},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

class _Lock:
    def __init__(self, handle: Any) -> None:
        self.handle = handle

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
