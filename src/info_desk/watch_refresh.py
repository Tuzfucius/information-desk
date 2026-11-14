"""Refresh due watched sources and detect verified code attachments."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from info_desk.config import Settings
from info_desk.exceptions import InfoDeskError
from info_desk.resolver import IdentityResolver
from info_desk.sources.base import AdapterError
from info_desk.sources.registry import AdapterRegistry
from info_desk.storage.repository import Repository

logger = logging.getLogger(__name__)


class WatchRefresher:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        adapters: dict[str, Any] | AdapterRegistry,
        resolver: IdentityResolver,
        *,
        upstream_error: Any = None,
        clock: Any = None,
        monotonic: Any = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.adapters = adapters if isinstance(adapters, AdapterRegistry) else AdapterRegistry.from_mapping(adapters)
        self.resolver = resolver
        self.upstream_error = upstream_error
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic

    def _now(self) -> str:
        return self._iso(self.clock())

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    async def refresh_watched(
        self, *, root_run_id: int, deadline: float
    ) -> dict[str, Any]:
        started = self._now()
        run_id = self.repository.start_run(
            started_at=started,
            root_run_id=root_run_id,
            job_key="watch_refresh",
        )
        cutoff = self._iso(self.clock())
        sources = self.repository.list_due_watched(
            cutoff, limit=self.settings.watch_refresh_limit_per_run
        )
        checked = failed = skipped = new_events = new_items = new_sources = conflict_count = 0
        error_codes: dict[str, int] = {}

        def record_failure(source: dict[str, Any], code: str, retry_after: float | None = None) -> None:
            nonlocal failed
            failed += 1
            error_codes[code] = error_codes.get(code, 0) + 1
            failures = int(source.get("consecutive_failures") or 0) + 1
            backoff_hours = min(
                self.settings.watch_failure_max_hours,
                self.settings.watch_failure_base_hours * (2 ** min(failures - 1, 16)),
            )
            if retry_after is not None:
                backoff_hours = min(
                    self.settings.watch_failure_max_hours,
                    max(backoff_hours, math.ceil(max(0, retry_after) / 3600)),
                )
            next_check = self._iso(self.clock() + timedelta(hours=backoff_hours))
            self.repository.record_watch_failure(
                int(source["id"]), now=self._now(), next_check_at=next_check, error_code=code
            )

        for source in sources:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            feed = source.get("feed")
            if (
                feed not in self.adapters
                or source["source_type"] not in self.adapters.capabilities(feed).source_types
            ):
                feed = self.adapters.feed_for_source_type(source["source_type"])
            if feed is None or feed not in self.adapters:
                interval = (
                    self.settings.watch_refresh_interval_hours
                    or int(source.get("refresh_interval_hours") or 24)
                )
                self.repository.defer_watch_source(
                    int(source["id"]),
                    next_check_at=self._iso(self.clock() + timedelta(hours=interval)),
                    reason="SOURCE_ADAPTER_UNAVAILABLE",
                )
                skipped += 1
                continue
            capabilities = self.adapters.capabilities(feed)
            if not capabilities.supports_inspect:
                interval = (
                    self.settings.watch_refresh_interval_hours
                    or int(source.get("refresh_interval_hours") or capabilities.watch_interval_hours)
                )
                self.repository.defer_watch_source(
                    int(source["id"]),
                    next_check_at=self._iso(self.clock() + timedelta(hours=interval)),
                    reason="INSPECTION_UNSUPPORTED",
                )
                skipped += 1
                continue
            adapter = self.adapters[feed]
            item_budget = min(
                self.settings.watch_item_budget_sec, capabilities.watch_timeout_seconds
            )
            item_deadline = min(deadline, self.monotonic() + item_budget)
            request_budget = item_deadline - self.monotonic()
            if request_budget <= 0:
                record_failure(source, "UPSTREAM_TIMEOUT")
                continue
            try:
                inspection = await asyncio.wait_for(
                    adapter.inspect(source["url"]),
                    timeout=request_budget,
                )
            except (AdapterError, InfoDeskError) as error:
                record_failure(source, getattr(error, "code", "UPSTREAM_UNAVAILABLE"), getattr(error, "retry_after_seconds", None))
                continue
            except (TimeoutError, httpx.TimeoutException):
                record_failure(source, "UPSTREAM_TIMEOUT")
                continue
            except httpx.HTTPStatusError as error:
                upstream = self._upstream_error(error.response)
                record_failure(
                    source, upstream.code, upstream.retry_after_seconds
                )
                continue
            except httpx.HTTPError:
                record_failure(source, "UPSTREAM_UNAVAILABLE")
                continue
            except Exception:
                logger.exception("Unexpected Watch inspection failure for source %s", source["id"])
                record_failure(source, "INTERNAL_ERROR")
                continue

            candidate = inspection.source
            if candidate.source_type not in capabilities.source_types:
                record_failure(source, "ADAPTER_CONTRACT_VIOLATION")
                continue
            candidate.watch_interval_hours = capabilities.watch_interval_hours
            if candidate.source_type == "github_repo":
                candidate.metadata["code_status"] = inspection.code_status
                candidate.metadata["code_signals"] = inspection.signals
            item = self.repository.get_item(source["item_id"])
            if item is None or not item["is_watched"]:
                continue
            now = self._now()
            candidate.feed = feed
            try:
                with self.repository.transaction() as connection:
                    ingested = self.resolver.ingest_candidate(
                        candidate,
                        conn=connection,
                        now=now,
                        run_id=run_id,
                        last_checked_at=now,
                    )
            except Exception:
                logger.exception("Failed to persist Watch result for source %s", source["id"])
                record_failure(source, "INTERNAL_ERROR")
                continue

            new_sources += int(ingested.source_created)
            new_items += int(ingested.item_created)
            new_events += ingested.event_count
            conflict_count += ingested.conflict_count
            checked += 1
            if ingested.item_id is None:
                record_failure(source, "MERGE_CONFLICT")
                continue

            interval_hours = (
                self.settings.watch_refresh_interval_hours
                or int(source.get("refresh_interval_hours") or capabilities.watch_interval_hours)
            )
            next_check_at = self._iso(self.clock() + timedelta(hours=interval_hours))
            self.repository.record_watch_success(
                int(source["id"]), now=now, next_check_at=next_check_at,
                interval_hours=interval_hours,
            )
            if ingested.source_id is not None and int(ingested.source_id) != int(source["id"]):
                self.repository.record_watch_success(
                    int(ingested.source_id), now=now, next_check_at=next_check_at,
                    interval_hours=interval_hours,
                )

            if candidate.source_type == "hf_paper" and "github" in self.adapters:
                targets = [
                    link["target_url"]
                    for link in self.resolver.normalized_links(candidate)
                    if link["target_source_type"] == "github_repo"
                    and link["relation_type"] == "official_code"
                ][:3]
                for target in targets:
                    remaining = item_deadline - self.monotonic()
                    if remaining <= 0:
                        record_failure(source, "UPSTREAM_TIMEOUT")
                        break
                    try:
                        repo_inspection = await asyncio.wait_for(
                            self.adapters["github"].inspect(target), timeout=remaining
                        )
                    except (AdapterError, InfoDeskError) as error:
                        record_failure(source, getattr(error, "code", "UPSTREAM_UNAVAILABLE"), getattr(error, "retry_after_seconds", None))
                        continue
                    except (TimeoutError, httpx.TimeoutException):
                        record_failure(source, "UPSTREAM_TIMEOUT")
                        continue
                    except httpx.HTTPStatusError as error:
                        upstream = self._upstream_error(error.response)
                        record_failure(
                            source, upstream.code, upstream.retry_after_seconds
                        )
                        continue
                    except httpx.HTTPError:
                        record_failure(source, "UPSTREAM_UNAVAILABLE")
                        continue
                    except Exception:
                        logger.exception("Unexpected linked GitHub inspection failure")
                        record_failure(source, "INTERNAL_ERROR")
                        continue

                    repo_candidate = repo_inspection.source
                    github_capabilities = self.adapters.capabilities("github")
                    if repo_candidate.source_type not in github_capabilities.source_types:
                        record_failure(source, "ADAPTER_CONTRACT_VIOLATION")
                        continue
                    repo_candidate.watch_interval_hours = github_capabilities.watch_interval_hours
                    repo_candidate.extracted_links = repo_inspection.extracted_links
                    reverse_links = self.resolver.normalized_links(repo_candidate)
                    references_paper = any(
                        link["target_source_type"] in {"arxiv", "hf_paper"}
                        and link["target_external_id"] == candidate.source_uid
                        for link in reverse_links
                    )
                    if not references_paper:
                        continue
                    repo_candidate.metadata["code_status"] = repo_inspection.code_status
                    repo_candidate.metadata["code_signals"] = repo_inspection.signals
                    repo_candidate.extracted_links.append(
                        {
                            "url": candidate.url,
                            "relation_type": "official_code",
                            "evidence_level": "strong",
                            "evidence_origin": "bidirectional_official_code",
                            "evidence_excerpt": candidate.source_uid,
                        }
                    )
                    repo_checked_at = self._now()
                    try:
                        with self.repository.transaction() as connection:
                            ingested = self.resolver.ingest_candidate(
                                repo_candidate,
                                conn=connection,
                                now=repo_checked_at,
                                run_id=run_id,
                                last_checked_at=repo_checked_at,
                            )
                    except Exception:
                        logger.exception("Failed to persist linked GitHub source")
                        record_failure(source, "INTERNAL_ERROR")
                        continue
                    new_events += ingested.event_count
                    new_sources += int(ingested.source_created)
                    new_items += int(ingested.item_created)
                    conflict_count += ingested.conflict_count
                    checked += 1
                    if ingested.item_id is None or ingested.conflict_count:
                        record_failure(source, "MERGE_CONFLICT")

        queue_status = self.repository.watch_queue_status(cutoff)
        pending_due = queue_status["due_count"] > 0
        if failed == 0 and skipped == 0 and not pending_due:
            status = "success"
        elif checked == 0 and failed == 0:
            status = "skipped"
        elif checked or skipped:
            status = "partial"
        else:
            status = "failed"
        finished = self._now()
        warnings = ([{"failed": failed, "skipped": skipped, "error_codes": error_codes}]
                    if failed or skipped else [])
        with self.repository.transaction() as connection:
            self.repository.update_run(
                run_id,
                ended_at=finished,
                status=status,
                fetched_count=checked,
                new_sources=new_sources,
                new_items=new_items,
                new_events=new_events,
                conflict_count=conflict_count,
                warnings=warnings,
                error_code=None if status == "success" else "WATCH_REFRESH_PARTIAL",
                conn=connection,
            )
        return {
            "last_attempt_at": started,
            "last_status": status,
            "checked": checked,
            "skipped": skipped,
            "failed": failed,
            "pending_due": pending_due,
            "due_count": queue_status["due_count"],
            "watched_sources": queue_status["watched_sources"],
            "failed_sources": queue_status["failed_sources"],
            "next_due_at": queue_status["next_due_at"],
            "new_items": new_items,
            "new_sources": new_sources,
            "new_events": new_events,
            "conflicts": conflict_count,
        }

    def _upstream_error(self, response: httpx.Response) -> InfoDeskError:
        if self.upstream_error is not None:
            return self.upstream_error(response)
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            try:
                seconds = float(retry_after) if retry_after else None
            except ValueError:
                seconds = None
            return InfoDeskError(
                "RATE_LIMITED", "The source rate limit was reached.",
                retry_after_seconds=seconds,
            )
        return InfoDeskError(
            "UPSTREAM_UNAVAILABLE", "The source request returned an unsuccessful response."
        )
