from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from info_desk.contracts import FetchRequest
from info_desk.resolver import IdentityResolver
from info_desk.sources.base import AdapterError, CandidateSource, PageResult
from info_desk.sync_engine import SyncEngine
from info_desk.watch_refresh import WatchRefresher


class WindowPages:
    capabilities = None  # use the arXiv compatibility registration

    def __init__(self, count: int = 25) -> None:
        self.records: list[CandidateSource] = []
        self.fail_right = False
        self.fail_right_once = False
        self.force_split = False
        self.crash_after_page = False
        self.count = count

    async def discover(self, _query, window, page_token=None):
        start, end = window
        if self.force_split and (end - start) > timedelta(days=1):
            return PageResult(
                records=[], page_complete=False, source_response_incomplete=True,
                warnings=["PARTIAL_SYNC"],
            )
        if self.fail_right and start >= self.right_start and self.fail_right_once:
            self.fail_right_once = False
            raise AdapterError("UPSTREAM_TIMEOUT", "fixture outage")
        records = [
            candidate for candidate in self.records
            if start <= datetime.fromisoformat(candidate.published_at) < end
        ]
        offset = int(page_token or 0)
        page = records[offset : offset + 1]
        next_offset = offset + 1
        complete = next_offset >= len(records)
        return PageResult(
            records=page,
            next_page_token=None if complete else str(next_offset),
            page_complete=complete,
        )


def _engine(settings, repository, adapter):
    resolver = IdentityResolver(repository)
    watch = WatchRefresher(settings, repository, {"arxiv": adapter}, resolver)
    return SyncEngine(settings, repository, {"arxiv": adapter}, resolver, watch, lambda _r: None)


def _records(adapter: WindowPages, window: tuple[datetime, datetime]) -> None:
    split = SyncEngine._split_window(window, 86_400)[0][1]
    left_time = split - timedelta(hours=12)
    right_time = split + timedelta(hours=12)
    for number in range(adapter.count):
        published = left_time if number < adapter.count // 2 else right_time
        uid = f"2611.{number:05d}"
        adapter.records.append(CandidateSource(
            source_type="arxiv", source_uid=uid, external_id=uid,
            url=f"https://arxiv.org/abs/{uid}", title=f"Item {number}",
            description=f"Description {number}",
            published_at=published.isoformat().replace("+00:00", "Z"),
        ))


async def test_twenty_five_pages_are_recovered_by_gapless_window_splitting(settings, repository):
    settings.max_pages_per_window = 20
    settings.sync_max_window_splits = 4
    adapter = WindowPages()
    sync = _engine(settings, repository, adapter)
    end = datetime.now(UTC).replace(microsecond=0)
    initial = (end - timedelta(days=2), end)
    _records(adapter, initial)

    result = await sync.run(FetchRequest(feeds=["arxiv"], days=2, topics=["VLA"]))

    assert result["status"] == "success"
    assert len(repository.search_items(include_ignored=True, limit=100)) == 25
    assert len(repository.list_events(after_id=0, limit=100)["events"]) == 25
    assert repository.list_sync_segments(result["jobs"][0]["job_key"]) == []
    assert result["jobs"][0]["stats"]["new_items"] == 25


async def test_completed_sibling_segment_survives_another_segment_failure_and_recovers(
    settings, repository
):
    settings.sync_max_window_splits = 4
    adapter = WindowPages(count=4)
    sync = _engine(settings, repository, adapter)
    end = datetime.now(UTC).replace(microsecond=0)
    initial = (end - timedelta(days=2), end)
    _records(adapter, initial)
    split = SyncEngine._split_window(initial, 86_400)[0][1]
    adapter.right_start = split
    adapter.fail_right = True
    adapter.fail_right_once = True
    adapter.force_split = True
    request = FetchRequest(feeds=["arxiv"], days=2, topics=["VLA"])

    first = await sync.run(request)
    key = first["jobs"][0]["job_key"]
    pending = repository.list_sync_segments(key)
    assert first["status"] == "partial"
    assert len(pending) == 1
    assert datetime.fromisoformat(pending[0]["window_start"]) >= split
    assert len(repository.search_items(include_ignored=True, limit=100)) == 2
    state = repository.get_sync_state(key)
    assert state["completed_until"] == split.isoformat().replace("+00:00", "Z")

    adapter.fail_right = False
    second = await sync.run(request)
    assert second["status"] == "success"
    assert repository.list_sync_segments(key) == []
    assert len(repository.search_items(include_ignored=True, limit=100)) == 4
    assert len(repository.list_events(after_id=0, limit=100)["events"]) == 4


async def test_page_commit_before_segment_ack_is_idempotent_after_restart(settings, repository):
    adapter = WindowPages(count=1)
    sync = _engine(settings, repository, adapter)
    end = datetime.now(UTC).replace(microsecond=0)
    _records(adapter, (end - timedelta(days=2), end))
    original = repository.complete_sync_segment
    first_call = True

    def crash_once(segment_id: int) -> None:
        nonlocal first_call
        if first_call:
            first_call = False
            raise RuntimeError("simulated process exit after page commit")
        original(segment_id)

    repository.complete_sync_segment = crash_once
    with pytest.raises(RuntimeError, match="simulated process exit"):
        await sync.run(FetchRequest(feeds=["arxiv"], days=2, topics=["VLA"]))
    repository.complete_sync_segment = original

    recovered = await sync.run(FetchRequest(feeds=["arxiv"], days=2, topics=["VLA"]))
    assert recovered["status"] == "success"
    assert len(repository.search_items(include_ignored=True, limit=100)) == 1
    assert len(repository.list_events(after_id=0, limit=100)["events"]) == 1
