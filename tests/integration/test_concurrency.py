from __future__ import annotations

import fcntl
import multiprocessing

import pytest

from info_desk.contracts import FetchRequest
from info_desk.exceptions import InfoDeskError
from info_desk.resolver import IdentityResolver
from info_desk.sources.base import PageResult
from info_desk.sync_engine import SyncEngine
from info_desk.watch_refresh import WatchRefresher


def _probe_lock(path: str, result_queue) -> None:
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result_queue.put("busy")
        else:
            result_queue.put("acquired")
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class EmptyAdapter:
    async def discover(self, query, window, page_token=None):
        return PageResult(records=[], page_complete=True)


def _engine(settings, repository):
    adapters = {"arxiv": EmptyAdapter()}
    resolver = IdentityResolver(repository)
    watch = WatchRefresher(settings, repository, adapters, resolver)
    return SyncEngine(settings, repository, adapters, resolver, watch, lambda _response: None)


def test_feed_lock_excludes_a_second_process(settings, repository) -> None:
    sync = _engine(settings, repository)
    queue = multiprocessing.get_context("fork").Queue()
    with sync._feed_lock("arxiv"):
        path = settings.home / "run" / "arxiv.lock"
        process = multiprocessing.get_context("fork").Process(
            target=_probe_lock, args=(str(path), queue)
        )
        process.start()
        assert queue.get(timeout=3) == "busy"
        process.join(timeout=3)
        assert process.exitcode == 0


async def test_manual_and_scheduled_sync_share_lock_and_release_after_run(settings, repository) -> None:
    sync = _engine(settings, repository)
    with sync._feed_lock("arxiv"), pytest.raises(InfoDeskError) as raised:
        await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]))
    assert raised.value.code == "SYNC_ALREADY_RUNNING"

    result = await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]))
    assert result["status"] == "success"


async def test_scheduled_watch_run_holds_all_registered_feed_locks(settings, repository) -> None:
    adapters = {"arxiv": EmptyAdapter(), "github": EmptyAdapter()}
    resolver = IdentityResolver(repository)
    refresher = WatchRefresher(settings, repository, adapters, resolver)
    sync = SyncEngine(settings, repository, adapters, resolver, refresher, lambda _r: None)

    with sync._feed_lock("github"), pytest.raises(InfoDeskError) as raised:
        await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]), scheduled=True)

    assert raised.value.code == "SYNC_ALREADY_RUNNING"


async def test_next_sync_recovers_interrupted_run_without_losing_waterline(settings, repository) -> None:
    sync = _engine(settings, repository)
    query = sync._queries_for_feed("arxiv", ["VLA"])[0]
    job_key = sync._job_key("arxiv", query, None)
    repository.upsert_sync_state(
        job_key,
        "arxiv",
        query,
        now="2026-01-01T00:00:00Z",
        coverage_from="2025-12-01T00:00:00Z",
        completed_until="2026-01-01T00:00:00Z",
        last_status="success",
    )
    root_id = repository.start_run(
        started_at="2026-01-02T00:00:00Z", job_key="root:arxiv"
    )
    repository.start_run(
        started_at="2026-01-02T00:00:00Z",
        root_run_id=root_id,
        job_key=job_key,
        feed="arxiv",
    )

    repository.recover_stale_runs(["arxiv"], now="2026-01-03T00:00:00Z")
    old_child = next(
        run
        for run in repository.list_runs(job_key=job_key, limit=10)
        if run["error_code"] == "INTERRUPTED"
    )
    state = repository.get_sync_state(job_key)
    assert old_child["status"] == "failed"
    assert old_child["error_code"] == "INTERRUPTED"
    assert state["last_status"] == "failed"
    assert state["last_success_at"] == "2026-01-01T00:00:00Z"
    assert state["completed_until"] == "2026-01-01T00:00:00Z"

    await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]), scheduled=True)
    assert repository.get_sync_state(job_key)["last_status"] == "success"
