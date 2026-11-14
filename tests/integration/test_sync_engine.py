from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta

from info_desk.contracts import FetchRequest
from info_desk.resolver import IdentityResolver
from info_desk.sources.base import AdapterError, CandidateSource, PageResult
from info_desk.sync_engine import SyncEngine
from info_desk.watch_refresh import WatchRefresher


def paper(number: int) -> CandidateSource:
    uid = f"2601.{number:05d}"
    return CandidateSource(
        source_type="arxiv",
        source_uid=uid,
        external_id=uid,
        url=f"https://arxiv.org/abs/{uid}",
        title=f"Paper {number}",
        description=f"Abstract {number}",
    )


class ScriptedAdapter:
    def __init__(self, discover: Callable | None = None) -> None:
        self.discover_call = discover
        self.windows: list[tuple[datetime, datetime]] = []

    async def discover(self, query, window, page_token=None):
        self.windows.append(window)
        if self.discover_call:
            return await self.discover_call(query, window, page_token)
        return PageResult(records=[], page_complete=True)


def engine(settings, repository, adapters, resolver=None):
    resolved = resolver or IdentityResolver(repository)
    refresher = WatchRefresher(settings, repository, adapters, resolved)
    return SyncEngine(settings, repository, adapters, resolved, refresher, lambda response: None)


async def test_first_success_sets_success_time_and_completed_waterline(settings, repository) -> None:
    adapter = ScriptedAdapter()
    sync = engine(settings, repository, {"arxiv": adapter})

    result = await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]))

    state = repository.list_sync_states()[0]
    assert result["status"] == "success"
    assert state["last_status"] == "success"
    assert state["last_success_at"] == state["last_attempt_at"]
    assert state["completed_until"] == result["jobs"][0]["window_end"]


async def test_third_page_failure_retains_prior_waterline_and_rerun_recovers(settings, repository) -> None:
    failed = True

    async def pages(_query, _window, token):
        nonlocal failed
        if token is None:
            return PageResult(records=[paper(1)], next_page_token="1", page_complete=False)
        if token == "1":
            return PageResult(records=[paper(2)], next_page_token="2", page_complete=False)
        if failed:
            raise AdapterError("UPSTREAM_TIMEOUT", "offline fixture timeout")
        return PageResult(records=[paper(3)], page_complete=True)

    adapter = ScriptedAdapter(pages)
    sync = engine(settings, repository, {"arxiv": adapter})
    request = FetchRequest(feeds=["arxiv"], topics=["VLA"])
    query = sync._queries_for_feed("arxiv", ["VLA"])[0]
    job_key = sync._job_key("arxiv", query, None)
    repository.upsert_sync_state(
        job_key, "arxiv", query,
        now="2025-12-30T00:00:00Z", coverage_from="2025-12-20T00:00:00Z",
        completed_until="2025-12-30T00:00:00Z", last_status="success",
    )

    partial = await sync.run(request, scheduled=True)
    state = next(value for value in repository.list_sync_states() if value["query"].get("topics") == ["VLA"])
    assert partial["status"] == "partial"
    assert partial["jobs"][0]["completed_until"] is None
    assert partial["jobs"][0]["stats"]["fetched"] == 2
    assert state["completed_until"] == "2025-12-30T00:00:00Z"
    assert state["last_success_at"] == "2025-12-30T00:00:00Z"
    assert len(repository.search_items(include_ignored=True, limit=20)) == 2

    failed = False
    recovered = await sync.run(request, scheduled=True)
    state = next(value for value in repository.list_sync_states() if value["query"].get("topics") == ["VLA"])
    assert recovered["status"] == "success"
    assert state["last_status"] == "success"
    assert state["completed_until"] == recovered["jobs"][0]["window_end"]
    assert len(repository.search_items(include_ignored=True, limit=20)) == 3
    assert len(repository.list_events(after_id=0, limit=20)["events"]) == 3


async def test_page_transaction_failure_rolls_back_records_and_statistics(settings, repository) -> None:
    class FailingResolver(IdentityResolver):
        calls = 0

        def ingest_candidate(self, candidate, **kwargs):
            result = super().ingest_candidate(candidate, **kwargs)
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("injected failure inside page transaction")
            return result

    async def one_page(_query, _window, _token):
        return PageResult(records=[paper(10), paper(11)], page_complete=True)

    adapter = ScriptedAdapter(one_page)
    sync = engine(settings, repository, {"arxiv": adapter}, FailingResolver(repository))

    result = await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]))

    assert result["status"] == "failed"
    assert result["jobs"][0]["error_code"] == "INTERNAL_ERROR"
    assert result["jobs"][0]["stats"] == {
        "fetched": 0,
        "new_items": 0,
        "new_sources": 0,
        "new_events": 0,
        "conflicts": 0,
    }
    assert repository.search_items(include_ignored=True, limit=20) == []
    assert repository.list_events(after_id=0, limit=20)["events"] == []


async def test_successful_scheduled_run_reuses_overlap_window(settings, repository) -> None:
    adapter = ScriptedAdapter()
    sync = engine(settings, repository, {"arxiv": adapter})
    request = FetchRequest(feeds=["arxiv"], topics=["VLA"], days=1)

    await sync.run(request, scheduled=True)
    previous_end = adapter.windows[-1][1]
    await sync.run(request, scheduled=True)

    second_start = adapter.windows[-1][0]
    assert (previous_end - second_start).total_seconds() == settings.overlap_hours * 3600


async def test_incomplete_github_window_refines_by_subday_ranges(settings, repository) -> None:
    settings.enabled_feeds = ["github"]
    settings.topics = ["VLA"]
    seen: list[tuple[datetime, datetime]] = []

    async def incomplete_once(_query, window, _token):
        seen.append(window)
        if len(seen) == 1:
            return PageResult(
                records=[],
                page_complete=False,
                source_response_incomplete=True,
                warnings=["GITHUB_INCOMPLETE_RESULTS"],
            )
        return PageResult(records=[], page_complete=True)

    adapter = ScriptedAdapter(incomplete_once)
    sync = engine(settings, repository, {"github": adapter})
    result = await sync.run(FetchRequest(feeds=["github"], topics=["VLA"], days=1))

    assert result["jobs"][0]["status"] == "success"
    # A GitHub feed runs separate created and updated searches.
    assert len(seen) == 4
    assert (seen[1][1] - seen[1][0]).total_seconds() == 12 * 3600
    assert (seen[2][1] - seen[2][0]).total_seconds() == 12 * 3600


async def test_unresolvable_github_window_remains_partial(settings, repository) -> None:
    settings.enabled_feeds = ["github"]
    settings.topics = ["VLA"]
    async def incomplete(_query, _window, _token):
        return PageResult(
            records=[], page_complete=False, source_response_incomplete=True,
            warnings=["GITHUB_INCOMPLETE_RESULTS"],
        )

    adapter = ScriptedAdapter(incomplete)
    sync = engine(settings, repository, {"github": adapter})
    now = datetime.now().astimezone().replace(microsecond=0)
    window = (now, now + timedelta(seconds=1))
    assert SyncEngine._split_github_window(window) is None

    root_run_id = repository.start_run(started_at="2026-01-01T00:00:00Z")
    job = await sync._run_discovery_job(
        "github", {"topic": "VLA", "mode": "created"}, window,
        root_run_id=root_run_id, deadline=10**20, job_key="small-github-window",
    )
    assert job["status"] == "partial"
    assert repository.get_sync_state("small-github-window")["completed_until"] is None


async def test_slow_source_does_not_consume_the_next_sources_budget(settings, repository) -> None:
    settings.enabled_feeds = ["arxiv", "hf_papers"]
    settings.refresh_budget_sec = 0.3

    async def slow(_query, _window, _token):
        await asyncio.sleep(1)
        return PageResult(records=[], page_complete=True)

    adapters = {"arxiv": ScriptedAdapter(slow), "hf_papers": ScriptedAdapter()}
    sync = engine(settings, repository, adapters)

    result = await sync.run(FetchRequest(feeds=["arxiv", "hf_papers"], topics=["VLA"]))

    assert result["jobs"][0]["error_code"] == "UPSTREAM_TIMEOUT"
    assert result["jobs"][1]["status"] == "success"
    assert result["status"] == "partial"


async def test_watch_phase_has_reserved_budget_after_discovery_timeout(settings, repository) -> None:
    settings.scheduled_budget_sec = 0.6
    settings.enabled_feeds = ["arxiv"]
    with repository.transaction() as connection:
        item_id = repository.create_item(
            {"title": "watched", "is_watched": True},
            now="2026-01-01T00:00:00Z",
            conn=connection,
        )
        repository.upsert_source(
            item_id,
            {
                "source_type": "arxiv",
                "source_uid": "2601.00222",
                "external_id": "2601.00222",
                "url": "https://arxiv.org/abs/2601.00222",
                "title": "watched",
            },
            now="2026-01-01T00:00:00Z",
            conn=connection,
        )

    async def slow_discovery(_query, _window, _token):
        await asyncio.sleep(1)
        return PageResult(records=[], page_complete=True)

    class WatchAdapter(ScriptedAdapter):
        async def inspect(self, target):
            from info_desk.sources.base import SourceInspection

            return SourceInspection(
                source=paper(222), checked_at="2026-11-14T00:00:00Z"
            )

    adapter = WatchAdapter(slow_discovery)
    sync = engine(settings, repository, {"arxiv": adapter})
    result = await sync.run(
        FetchRequest(feeds=["arxiv"], topics=["VLA"]), scheduled=True
    )

    assert result["jobs"][0]["error_code"] == "UPSTREAM_TIMEOUT"
    assert result["watch_refresh"]["last_status"] == "success"
    assert result["watch_refresh"]["checked"] == 1
