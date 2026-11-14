from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from info_desk.resolver import IdentityResolver
from info_desk.sources.base import (
    AdapterError,
    CandidateSource,
    SourceCapabilities,
    SourceInspection,
)
from info_desk.sources.registry import AdapterRegistration, AdapterRegistry
from info_desk.watch_refresh import WatchRefresher


def _candidate(index: int) -> CandidateSource:
    uid = f"2501.{index:05d}"
    return CandidateSource(
        source_type="arxiv", source_uid=uid, external_id=uid,
        url=f"https://arxiv.org/abs/{uid}", title=f"Paper {index}",
        description="Watch queue fixture",
    )


class InspectAdapter:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[str] = []

    async def inspect(self, target: str) -> SourceInspection:
        self.calls.append(target)
        if self.failures:
            self.failures -= 1
            raise AdapterError(
                "RATE_LIMITED", "fixture throttled", retry_after_seconds=10**7
            )
        index = int(target.rsplit(".", 1)[-1])
        candidate = _candidate(index)
        return SourceInspection(candidate, checked_at="2026-02-01T00:00:00Z")


def _seed_watched(repository, count: int) -> None:
    with repository.transaction() as connection:
        for index in range(count):
            uid = f"2501.{index:05d}"
            item_id = repository.create_item(
                {"title": f"Paper {index}", "is_watched": True},
                now="2026-01-01T00:00:00Z", conn=connection,
            )
            repository.upsert_source(
                item_id,
                {
                    "source_type": "arxiv", "source_uid": uid,
                    "external_id": uid,
                    "url": f"https://arxiv.org/abs/{uid}", "title": f"Paper {index}",
                },
                now="2026-01-01T00:00:00Z", conn=connection,
            )


@pytest.mark.parametrize("count", [100, 500])
async def test_watch_queue_is_bounded_and_drains_fairly_across_runs(
    settings, repository, count: int
):
    settings.watch_refresh_limit_per_run = 100
    now = [datetime(2026, 2, 1, tzinfo=UTC)]
    adapter = InspectAdapter()
    refresher = WatchRefresher(
        settings, repository, {"arxiv": adapter}, IdentityResolver(repository),
        clock=lambda: now[0], monotonic=lambda: 0.0,
    )
    _seed_watched(repository, count)
    observed = []

    for turn in range((count + 99) // 100):
        root_run_id = repository.start_run(started_at=f"2026-02-01T00:0{turn}:00Z")
        result = await refresher.refresh_watched(root_run_id=root_run_id, deadline=60.0)
        observed.append(result["checked"])
        assert result["checked"] <= 100
        if turn < (count + 99) // 100 - 1:
            assert result["pending_due"] is True

    queue = repository.watch_queue_status("2026-02-01T00:00:00Z")
    assert sum(observed) == count
    assert queue["due_count"] == 0
    assert len(adapter.calls) == count
    assert len(set(adapter.calls)) == count


async def test_watch_rate_limit_is_bounded_by_failure_policy_then_recovers(settings, repository):
    settings.watch_failure_base_hours = 1
    settings.watch_failure_max_hours = 24
    now = [datetime(2026, 2, 1, tzinfo=UTC)]
    _seed_watched(repository, 1)
    adapter = InspectAdapter(failures=1)
    refresher = WatchRefresher(
        settings, repository, {"arxiv": adapter}, IdentityResolver(repository),
        clock=lambda: now[0], monotonic=lambda: 0.0,
    )
    root_run_id = repository.start_run(started_at="2026-02-01T00:00:00Z")

    failed = await refresher.refresh_watched(root_run_id=root_run_id, deadline=60.0)
    source = repository.get_source("arxiv", "2501.00000")
    assert failed["last_status"] == "failed"
    assert source["consecutive_failures"] == 1
    assert source["last_error_code"] == "RATE_LIMITED"
    assert datetime.fromisoformat(source["next_check_at"]) <= now[0] + timedelta(hours=24)
    assert source["last_checked_at"] is None

    now[0] += timedelta(hours=25)
    root_run_id = repository.start_run(started_at="2026-02-02T01:00:00Z")
    recovered = await refresher.refresh_watched(root_run_id=root_run_id, deadline=60.0)
    source = repository.get_source("arxiv", "2501.00000")
    assert recovered["last_status"] == "success"
    assert source["consecutive_failures"] == 0
    assert source["last_error_code"] is None
    assert source["last_check_success_at"] is not None


async def test_watch_http_429_preserves_retry_after_error_classification(settings, repository):
    now = datetime(2026, 2, 1, tzinfo=UTC)
    _seed_watched(repository, 1)

    class Http429Adapter(InspectAdapter):
        async def inspect(self, target: str) -> SourceInspection:
            request = httpx.Request("GET", target)
            response = httpx.Response(
                429, headers={"Retry-After": "3600"}, request=request
            )
            raise httpx.HTTPStatusError(
                "limited", request=request, response=response
            )

    refresher = WatchRefresher(
        settings, repository, {"arxiv": Http429Adapter()}, IdentityResolver(repository),
        clock=lambda: now, monotonic=lambda: 0.0,
    )
    root_run_id = repository.start_run(started_at="2026-02-01T00:00:00Z")
    result = await refresher.refresh_watched(root_run_id=root_run_id, deadline=60.0)

    source = repository.get_source("arxiv", "2501.00000")
    assert result["last_status"] == "failed"
    assert source["last_error_code"] == "RATE_LIMITED"
    assert source["consecutive_failures"] == 1
    assert source["next_check_at"] == "2026-02-01T01:00:00Z"


async def test_source_unwatched_before_due_query_is_not_refreshed(settings, repository):
    _seed_watched(repository, 1)
    item = repository.search_items(is_watched=True, include_ignored=True, limit=1)[0]
    repository.update_item(
        item["item_id"], item["revision"], {"is_watched": False},
        now="2026-02-01T00:00:00Z",
    )
    adapter = InspectAdapter()
    refresher = WatchRefresher(
        settings, repository, {"arxiv": adapter}, IdentityResolver(repository),
        clock=lambda: datetime(2026, 2, 1, tzinfo=UTC), monotonic=lambda: 0.0,
    )
    root_run_id = repository.start_run(started_at="2026-02-01T00:00:00Z")
    result = await refresher.refresh_watched(root_run_id=root_run_id, deadline=60.0)
    assert result["checked"] == 0
    assert adapter.calls == []


async def test_watch_defers_sources_without_inspection_support(settings, repository):
    now = datetime(2026, 2, 1, tzinfo=UTC)
    _seed_watched(repository, 1)
    adapter = InspectAdapter()
    registry = AdapterRegistry([
        AdapterRegistration(
            "arxiv",
            adapter,
            SourceCapabilities(source_types=("arxiv",), supports_inspect=False),
        )
    ])
    refresher = WatchRefresher(
        settings, repository, registry, IdentityResolver(repository),
        clock=lambda: now, monotonic=lambda: 0.0,
    )

    result = await refresher.refresh_watched(
        root_run_id=repository.start_run(started_at="2026-02-01T00:00:00Z"),
        deadline=60.0,
    )

    source = repository.get_source("arxiv", "2501.00000")
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert adapter.calls == []
    assert source["consecutive_failures"] == 0
    assert source["last_checked_at"] is None
    assert source["next_check_at"] == "2026-02-02T00:00:00Z"


async def test_watch_defers_sources_after_their_adapter_is_removed(settings, repository):
    now = datetime(2026, 2, 1, tzinfo=UTC)
    _seed_watched(repository, 1)
    adapter = InspectAdapter()
    capabilities = SourceCapabilities(source_types=("test_notice",))
    registry = AdapterRegistry([
        AdapterRegistration("test_stream", adapter, capabilities)
    ])
    refresher = WatchRefresher(
        settings, repository, registry, IdentityResolver(repository),
        clock=lambda: now, monotonic=lambda: 0.0,
    )

    result = await refresher.refresh_watched(
        root_run_id=repository.start_run(started_at="2026-02-01T00:00:00Z"),
        deadline=60.0,
    )

    source = repository.get_source("arxiv", "2501.00000")
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["failed_sources"] == 0
    assert source["last_error_code"] == "SOURCE_ADAPTER_UNAVAILABLE"
    assert source["consecutive_failures"] == 0
    assert source["next_check_at"] == "2026-02-02T00:00:00Z"


async def test_watch_routes_sources_after_feed_rename(settings, repository):
    now = datetime(2026, 2, 1, tzinfo=UTC)
    _seed_watched(repository, 1)
    adapter = InspectAdapter()
    registry = AdapterRegistry([
        AdapterRegistration(
            "papers_v2", adapter, SourceCapabilities(source_types=("arxiv",))
        )
    ])
    refresher = WatchRefresher(
        settings, repository, registry, IdentityResolver(repository),
        clock=lambda: now, monotonic=lambda: 0.0,
    )

    result = await refresher.refresh_watched(
        root_run_id=repository.start_run(started_at="2026-02-01T00:00:00Z"),
        deadline=60.0,
    )

    source = repository.get_source("arxiv", "2501.00000")
    assert result["checked"] == 1
    assert adapter.calls == ["https://arxiv.org/abs/2501.00000"]
    assert source["feed"] == "papers_v2"
