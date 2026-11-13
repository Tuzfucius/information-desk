"""Refresh due watched sources and detect verified code attachments."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from info_desk.config import Settings
from info_desk.exceptions import InfoDeskError
from info_desk.resolver import IdentityResolver
from info_desk.sources.base import AdapterError
from info_desk.storage.repository import Repository


class WatchRefresher:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        adapters: dict[str, Any],
        resolver: IdentityResolver,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.adapters = adapters
        self.resolver = resolver

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

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
        cutoff = self._iso(
            datetime.now(UTC)
            - timedelta(hours=self.settings.watch_refresh_interval_hours)
        )
        sources = self.repository.list_due_watched(
            cutoff, limit=self.settings.watch_refresh_limit_per_run
        )
        checked = 0
        failed = 0
        new_events = 0
        new_items = 0
        new_sources = 0
        conflict_count = 0
        for index, source in enumerate(sources):
            if time.monotonic() >= deadline:
                failed += len(sources) - index
                break
            try:
                adapter = self.adapters[
                    {
                        "arxiv": "arxiv",
                        "hf_paper": "hf_papers",
                        "hf_model": "hf_models",
                        "github_repo": "github",
                    }[source["source_type"]]
                ]
                inspection = await adapter.inspect(source["url"])
            except (AdapterError, httpx.HTTPError, InfoDeskError):
                failed += 1
                continue
            candidate = inspection.source
            if candidate.source_type == "github_repo":
                candidate.metadata["code_status"] = inspection.code_status
                candidate.metadata["code_signals"] = inspection.signals
            item = self.repository.get_item(source["item_id"])
            if item is None:
                failed += 1
                continue
            now = self._now()
            with self.repository.transaction() as connection:
                _, _, source_created, item_created, added_events, conflicts = self.resolver.ingest_candidate(
                    candidate,
                    conn=connection,
                    now=now,
                    run_id=run_id,
                    last_checked_at=now,
                )
            new_sources += int(source_created)
            new_items += int(item_created)
            new_events += added_events
            conflict_count += conflicts
            checked += 1
            if candidate.source_type == "hf_paper":
                targets = [
                    link["url"]
                    for link in self.resolver.normalized_links(candidate)
                    if link["target_source_type"] == "github_repo"
                    and link["relation_type"] == "official_code"
                ][:3]
                for target in targets:
                    try:
                        repo_inspection = await self.adapters["github"].inspect(target)
                    except (AdapterError, httpx.HTTPError, InfoDeskError):
                        failed += 1
                        continue
                    repo_candidate = repo_inspection.source
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
                    with self.repository.transaction() as connection:
                        _, _, source_created, item_created, added_events, conflicts = self.resolver.ingest_candidate(
                            repo_candidate,
                            conn=connection,
                            now=repo_checked_at,
                            run_id=run_id,
                            last_checked_at=repo_checked_at,
                        )
                    new_events += added_events
                    new_sources += int(source_created)
                    new_items += int(item_created)
                    conflict_count += conflicts
                    checked += 1
                    if conflicts:
                        failed += 1

        status = "success" if failed == 0 else ("partial" if checked else "failed")
        finished = self._now()
        self.repository.update_run(
            run_id,
            ended_at=finished,
            status=status,
            fetched_count=checked,
            new_sources=new_sources,
            new_items=new_items,
            new_events=new_events,
            conflict_count=conflict_count,
            warnings=[{"failed": failed}] if failed else [],
            error_code=None if status == "success" else "WATCH_REFRESH_PARTIAL",
        )
        pending_due = bool(
            self.repository.list_due_watched(
                cutoff, limit=1
            )
        )
        return {
            "last_attempt_at": started,
            "last_status": status,
            "checked": checked,
            "failed": failed,
            "pending_due": pending_due,
            "new_items": new_items,
            "new_sources": new_sources,
            "new_events": new_events,
            "conflicts": conflict_count,
        }
