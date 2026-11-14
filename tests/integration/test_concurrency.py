from __future__ import annotations

from contextlib import contextmanager

import pytest

from info_desk.contracts import FetchRequest
from info_desk.exceptions import InfoDeskError
from info_desk.locking import LockConflictError, LockOperationError
from info_desk.resolver import IdentityResolver
from info_desk.sources.base import PageResult
from info_desk.sync_engine import SyncEngine
from info_desk.watch_refresh import WatchRefresher


class EmptyAdapter:
    async def discover(self, query, window, page_token=None):
        return PageResult(records=[], page_complete=True)


def _engine(settings, repository):
    adapters = {"arxiv": EmptyAdapter()}
    resolver = IdentityResolver(repository)
    watch = WatchRefresher(settings, repository, adapters, resolver)
    return SyncEngine(settings, repository, adapters, resolver, watch, lambda _response: None)


async def test_manual_and_scheduled_sync_share_lock_and_release_after_run(settings, repository) -> None:
    sync = _engine(settings, repository)
    with sync._feed_lock("arxiv"), pytest.raises(InfoDeskError) as raised:
        await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]))
    assert raised.value.code == "SYNC_ALREADY_RUNNING"

    result = await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]))
    assert result["status"] == "success"


async def test_scheduled_watch_run_holds_all_registered_feed_locks(settings, repository) -> None:
    adapters = {
        "arxiv": EmptyAdapter(),
        "github": EmptyAdapter(),
        "hf_models": EmptyAdapter(),
    }
    resolver = IdentityResolver(repository)
    refresher = WatchRefresher(settings, repository, adapters, resolver)
    sync = SyncEngine(settings, repository, adapters, resolver, refresher, lambda _r: None)

    with sync._feed_lock("github"), pytest.raises(InfoDeskError) as raised:
        await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]), scheduled=True)

    assert raised.value.code == "SYNC_ALREADY_RUNNING"
    with sync._feed_lock("arxiv"):
        pass


@pytest.mark.parametrize(
    ("lock_error", "expected_code"),
    [
        (PermissionError("denied"), "SYNC_LOCK_PERMISSION"),
        (OSError("disk error"), "SYNC_LOCK_IO"),
        (LockOperationError("backend error"), "SYNC_LOCK_ERROR"),
    ],
)
async def test_lock_operation_errors_are_not_mapped_to_conflicts(
    settings, repository, monkeypatch, lock_error, expected_code
) -> None:
    sync = _engine(settings, repository)

    def fail(_feed):
        raise lock_error

    monkeypatch.setattr(sync, "_feed_lock", fail)
    with pytest.raises(InfoDeskError) as raised:
        await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]))

    assert raised.value.code == expected_code


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


async def test_successful_sync_reports_release_failure_without_rewriting_committed_state(
    settings, repository, monkeypatch
) -> None:
    sync = _engine(settings, repository)

    @contextmanager
    def broken_release(_feed):
        yield
        raise LockOperationError("release backend failed")

    monkeypatch.setattr(sync, "_feed_lock", broken_release)
    with pytest.raises(InfoDeskError) as raised:
        await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]))
    assert raised.value.code == "SYNC_LOCK_RELEASE_FAILED"
    runs = repository.list_runs(limit=10)
    assert runs[0]["status"] == "success"


async def test_business_error_survives_release_error_and_logs_secondary_failure(
    settings, repository, monkeypatch, caplog
) -> None:
    sync = _engine(settings, repository)

    @contextmanager
    def broken_release(_feed):
        yield
        raise LockOperationError("release backend failed")

    def fail_recovery(*_args, **_kwargs):
        raise RuntimeError("primary sync failure")

    monkeypatch.setattr(sync, "_feed_lock", broken_release)
    monkeypatch.setattr(repository, "recover_stale_runs", fail_recovery)
    with pytest.raises(RuntimeError, match="primary sync failure"):
        await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]))
    assert "Failed to release synchronization locks after primary failure" in caplog.text


async def test_partial_acquisition_conflict_survives_release_errors_and_attempts_all_cleanup(
    settings, repository, monkeypatch, caplog
) -> None:
    adapters = {
        "arxiv": EmptyAdapter(),
        "github": EmptyAdapter(),
        "hf_models": EmptyAdapter(),
    }
    resolver = IdentityResolver(repository)
    watch = WatchRefresher(settings, repository, adapters, resolver)
    sync = SyncEngine(settings, repository, adapters, resolver, watch, lambda _r: None)
    released = []

    @contextmanager
    def first(_feed):
        try:
            yield
        finally:
            released.append("arxiv")
            raise LockOperationError("cleanup failed")

    @contextmanager
    def second(_feed):
        try:
            yield
        finally:
            released.append("github")
            raise LockOperationError("second cleanup failed")

    @contextmanager
    def third(_feed):
        raise LockConflictError("held")
        yield

    monkeypatch.setattr(
        sync,
        "_feed_lock",
        lambda feed: first(feed) if feed == "arxiv" else second(feed) if feed == "github" else third(feed),
    )
    with pytest.raises(InfoDeskError) as raised:
        await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]), scheduled=True)
    assert raised.value.code == "SYNC_ALREADY_RUNNING"
    assert set(released) == {"arxiv", "github"}
    assert "Failed to release partially acquired synchronization locks" in caplog.text
