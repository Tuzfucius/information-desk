from __future__ import annotations

from dataclasses import replace

import pytest

from info_desk.contracts import FetchRequest, SearchRequest
from info_desk.resolver import IdentityResolver
from info_desk.sources.base import AdapterError, CandidateSource, PageResult, SourceCapabilities
from info_desk.sources.registry import AdapterRegistration, AdapterRegistry
from info_desk.sync_engine import SyncEngine
from info_desk.watch_refresh import WatchRefresher


class FakeCursorAdapter:
    """A cursor-only, non-paper source used to exercise the extension contract."""

    capabilities = SourceCapabilities(
        source_types=("test_notice",),
        discovery_mode="cursor",
        window_resolution_seconds=None,
        window_filtering="none",
        durable_cursor=True,
        supports_inspect=False,
        watch_interval_hours=3,
        title_priorities={"test_notice": 10},
        query_builder=lambda topics: [{"topic": topic} for topic in topics],
    )

    def __init__(self) -> None:
        self.fail_once = True
        self.tokens: list[str | None] = []
        self.total = 3

    def normalize(self, raw: dict) -> CandidateSource:
        uid = str(raw["id"])
        return CandidateSource(
            source_type="test_notice", source_uid=uid, external_id=uid,
            url=f"https://example.test/notices/{uid}", title=raw["title"],
            description=raw["summary"],
        )


    async def discover(self, query, _window, page_token=None):
        self.tokens.append(page_token)
        if page_token == "cursor-1" and self.fail_once:
            self.fail_once = False
            raise AdapterError("RATE_LIMITED", "fixture rate limit", retry_after_seconds=30)
        offset = int(page_token.rsplit("-", 1)[1]) if page_token else 0
        if offset >= self.total:
            return PageResult(
                records=[], page_complete=True, checkpoint_token=f"cursor-{self.total}"
            )
        current = self.normalize(
            {"id": str(offset), "title": f"Notice {offset}", "summary": "Update"}
        )
        next_token = f"cursor-{offset + 1}" if offset < self.total - 1 else None
        return PageResult(
            records=[current], next_page_token=next_token,
            page_complete=next_token is None, resume_safe=True,
            checkpoint_token=f"cursor-{offset + 1}" if next_token is None else None,
        )


def test_registry_rejects_invalid_source_type_identifiers():
    adapter = FakeCursorAdapter()
    invalid = replace(adapter.capabilities, source_types=("release-notice",))

    with pytest.raises(ValueError, match="valid source type identifiers"):
        AdapterRegistry([AdapterRegistration("test_stream", adapter, invalid)])


async def test_fake_cursor_source_uses_registry_and_recovers_durable_cursor(settings, repository):
    settings.enabled_feeds = ["test_stream"]
    settings.topics = ["release"]
    adapter = FakeCursorAdapter()
    registry = AdapterRegistry([
        AdapterRegistration("test_stream", adapter, adapter.capabilities)
    ])
    resolver = IdentityResolver(repository, {"test_notice": 10})
    watch = WatchRefresher(settings, repository, registry, resolver)
    sync = SyncEngine(settings, repository, registry, resolver, watch, lambda _r: None)
    request = FetchRequest(feeds=["test_stream"], topics=["release"])

    partial = await sync.run(request)
    key = partial["jobs"][0]["job_key"]
    pending = repository.list_sync_segments(key)
    assert partial["status"] == "partial"
    assert partial["jobs"][0]["error_code"] == "RATE_LIMITED"
    assert pending[0]["page_token"] == "cursor-1"
    assert len(repository.search_items(keyword="Notice", include_ignored=True, limit=10)) == 1

    recovered = await sync.run(request)
    assert recovered["status"] == "success"
    assert len(repository.search_items(keyword="Notice", include_ignored=True, limit=10)) == 3
    assert len(repository.list_events(after_id=0, limit=10)["events"]) == 3
    assert repository.get_sync_state(key)["last_status"] == "success"
    assert repository.get_sync_state(key)["provider_cursor"] == "cursor-3"
    assert repository.get_sync_state(key)["completed_until"] is None
    assert registry.feed_for_source_type("test_notice") == "test_stream"
    assert SearchRequest(source_type="test_notice").source_type == "test_notice"

    adapter.total = 4
    incremental = await sync.run(request)
    assert incremental["status"] == "success"
    assert adapter.tokens[-1] == "cursor-3"
    assert len(repository.search_items(keyword="Notice", include_ignored=True, limit=10)) == 4
    assert len(repository.list_events(after_id=0, limit=10)["events"]) == 4


async def test_durable_cursor_advances_without_exceeding_per_run_page_cap(settings, repository):
    settings.enabled_feeds = ["test_stream"]
    settings.topics = ["release"]
    settings.max_pages_per_window = 1
    adapter = FakeCursorAdapter()
    adapter.fail_once = False
    registry = AdapterRegistry([
        AdapterRegistration("test_stream", adapter, adapter.capabilities)
    ])
    resolver = IdentityResolver(repository, {"test_notice": 10})
    watch = WatchRefresher(settings, repository, registry, resolver)
    sync = SyncEngine(settings, repository, registry, resolver, watch, lambda _r: None)
    request = FetchRequest(feeds=["test_stream"], topics=["release"])

    results = [await sync.run(request) for _ in range(3)]

    assert [result["jobs"][0]["status"] for result in results] == [
        "partial", "partial", "success"
    ]
    assert adapter.tokens == [None, "cursor-1", "cursor-2"]
    assert len(repository.search_items(keyword="Notice", include_ignored=True, limit=10)) == 3
    assert len(repository.list_events(after_id=0, limit=10)["events"]) == 3


async def test_cursor_source_without_checkpoint_stays_partial(settings, repository):
    settings.enabled_feeds = ["test_stream"]
    adapter = FakeCursorAdapter()
    adapter.fail_once = False

    async def missing_checkpoint(query, window, page_token=None):
        adapter.tokens.append(page_token)
        return PageResult(records=[], page_complete=True)

    adapter.discover = missing_checkpoint
    registry = AdapterRegistry([
        AdapterRegistration("test_stream", adapter, adapter.capabilities)
    ])
    resolver = IdentityResolver(repository, {"test_notice": 10})
    watch = WatchRefresher(settings, repository, registry, resolver)
    sync = SyncEngine(settings, repository, registry, resolver, watch, lambda _r: None)

    result = await sync.run(FetchRequest(feeds=["test_stream"], topics=["release"]))
    state = repository.get_sync_state(result["jobs"][0]["job_key"])
    pending = repository.list_sync_segments(result["jobs"][0]["job_key"])

    assert result["status"] == "partial"
    assert result["jobs"][0]["status"] == "partial"
    assert result["jobs"][0]["error_code"] == "CURSOR_CHECKPOINT_MISSING"
    assert state["provider_cursor"] is None
    assert state["completed_until"] is None
    assert pending[0]["last_error_code"] == "CURSOR_CHECKPOINT_MISSING"


async def test_adapter_cannot_persist_an_undeclared_source_type(settings, repository):
    settings.enabled_feeds = ["test_stream"]
    adapter = FakeCursorAdapter()

    async def wrong_source_type(query, window, page_token=None):
        return PageResult(
            records=[CandidateSource(
                source_type="unregistered_type", source_uid="1", external_id="1",
                url="https://example.test/1", title="Wrong type",
            )],
            page_complete=True,
            checkpoint_token="cursor-1",
        )

    adapter.discover = wrong_source_type
    registry = AdapterRegistry([
        AdapterRegistration("test_stream", adapter, adapter.capabilities)
    ])
    resolver = IdentityResolver(repository, {"test_notice": 10})
    watch = WatchRefresher(settings, repository, registry, resolver)
    sync = SyncEngine(settings, repository, registry, resolver, watch, lambda _r: None)

    result = await sync.run(FetchRequest(feeds=["test_stream"], topics=["release"]))

    assert result["status"] == "failed"
    assert result["jobs"][0]["error_code"] == "ADAPTER_CONTRACT_VIOLATION"
    assert repository.search_items(include_ignored=True, limit=10) == []
    assert repository.list_events(after_id=0, limit=10)["events"] == []


async def test_poll_adapter_success_does_not_claim_time_window_coverage(settings, repository):
    class PollAdapter:
        capabilities = SourceCapabilities(
            source_types=("poll_notice",),
            discovery_mode="poll",
            window_filtering="none",
            supports_inspect=False,
        )

        async def discover(self, query, window, page_token=None):
            return PageResult(records=[], page_complete=True)

    settings.enabled_feeds = ["poll_stream"]
    adapter = PollAdapter()
    registry = AdapterRegistry([
        AdapterRegistration("poll_stream", adapter, adapter.capabilities)
    ])
    resolver = IdentityResolver(repository)
    watch = WatchRefresher(settings, repository, registry, resolver)
    sync = SyncEngine(settings, repository, registry, resolver, watch, lambda _r: None)

    result = await sync.run(FetchRequest(feeds=["poll_stream"], topics=["notice"]))
    state = repository.get_sync_state(result["jobs"][0]["job_key"])

    assert result["status"] == "success"
    assert result["jobs"][0]["completed_until"] is None
    assert state["coverage_from"] is None
    assert state["completed_until"] is None


async def test_adapter_without_discovery_is_persisted_as_skipped(settings, repository):
    class InspectOnlyAdapter:
        capabilities = SourceCapabilities(
            source_types=("notice_detail",),
            discovery_mode="none",
            window_filtering="none",
            supports_inspect=True,
        )

        async def discover(self, query, window, page_token=None):
            raise AssertionError("a source without discovery support must not be called")

    settings.enabled_feeds = ["notice_detail"]
    adapter = InspectOnlyAdapter()
    registry = AdapterRegistry([
        AdapterRegistration("notice_detail", adapter, adapter.capabilities)
    ])
    resolver = IdentityResolver(repository)
    watch = WatchRefresher(settings, repository, registry, resolver)
    sync = SyncEngine(settings, repository, registry, resolver, watch, lambda _r: None)

    result = await sync.run(FetchRequest(feeds=["notice_detail"], topics=["notice"]))
    state = repository.get_sync_state(result["jobs"][0]["job_key"])

    assert result["status"] == "skipped"
    assert result["jobs"][0]["status"] == "skipped"
    assert state["last_status"] == "skipped"
    assert state["last_error"] == "DISCOVERY_UNSUPPORTED"
