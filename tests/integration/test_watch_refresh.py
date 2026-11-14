from __future__ import annotations

import time
from datetime import UTC, datetime

from info_desk.resolver import IdentityResolver
from info_desk.sources.base import CandidateSource, SourceInspection
from info_desk.watch_refresh import WatchRefresher


def _candidate(source_type: str, source_uid: str, **kwargs) -> CandidateSource:
    return CandidateSource(
        source_type=source_type,
        source_uid=source_uid,
        external_id=kwargs.pop("external_id", source_uid),
        url=kwargs.pop("url", f"https://example.test/{source_uid}"),
        title=kwargs.pop("title", source_uid),
        description=kwargs.pop("description", ""),
        **kwargs,
    )


class InspectAdapter:
    def __init__(self, callback):
        self.callback = callback

    async def inspect(self, target):
        return await self.callback(target)


async def test_old_watched_paper_detects_new_official_code_and_event(repository, settings) -> None:
    resolver = IdentityResolver(repository)
    checked_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    paper = _candidate(
        "arxiv",
        "2401.00123",
        url="https://arxiv.org/abs/2401.00123",
        title="Watched research",
    )
    with repository.transaction() as connection:
        paper_result = resolver.ingest_candidate(
            paper, conn=connection, now="2026-01-01T00:00:00Z", run_id=None
        )
        repository.update_item(
            paper_result.item_id,
            0,
            {"triage_status": "saved", "is_watched": True, "notes": "keep following"},
            now="2026-01-02T00:00:00Z",
            conn=connection,
        )
        repository.upsert_source(
            paper_result.item_id,
            {**paper.to_dict(), "last_checked_at": checked_at},
            now=checked_at,
            conn=connection,
        )

    hf_paper = _candidate(
        "hf_paper",
        "2401.00123",
        url="https://huggingface.co/papers/2401.00123",
        title="Watched research",
        extracted_links=[
            {
                "url": "https://github.com/org/official-code",
                "relation_type": "official_code",
                "evidence_level": "supporting",
                "evidence_origin": "hf-daily-papers",
            }
        ],
    )
    with repository.transaction() as connection:
        resolver.ingest_candidate(
            hf_paper, conn=connection, now="2026-11-13T00:00:00Z", run_id=None
        )

    async def inspect_hf(_target: str) -> SourceInspection:
        return SourceInspection(source=hf_paper, checked_at="2026-11-14T00:00:00Z")

    async def inspect_github(_target: str) -> SourceInspection:
        repo = _candidate(
            "github_repo",
            "992233",
            external_id="org/official-code",
            url="https://github.com/org/official-code",
            title="org/official-code",
            metadata={},
        )
        return SourceInspection(
            source=repo,
            checked_at="2026-11-14T00:00:00Z",
            code_status="detected",
            signals=["file:train.py"],
            extracted_links=[
                {
                    "url": "https://arxiv.org/abs/2401.00123",
                    "relation_type": "references",
                    "evidence_level": "weak",
                    "evidence_origin": "readme",
                }
            ],
        )

    async def unused(_target: str) -> SourceInspection:
        raise AssertionError("No other source should be refreshed.")

    adapters = {
        "arxiv": InspectAdapter(unused),
        "hf_papers": InspectAdapter(inspect_hf),
        "hf_models": InspectAdapter(unused),
        "github": InspectAdapter(inspect_github),
    }
    refresher = WatchRefresher(settings, repository, adapters, resolver)
    root_run = repository.start_run(started_at="2026-11-14T00:00:00Z")

    result = await refresher.refresh_watched(root_run_id=root_run, deadline=time.monotonic() + 5)

    item = repository.get_item(paper_result.item_id)
    assert result["last_status"] == "success"
    assert result["new_sources"] == 1
    assert item["triage_status"] == "saved"
    assert item["is_watched"] is True
    assert item["notes"] == "keep following"
    assert item["code_status"] == "detected"
    event_types = [event["event_type"] for event in repository.list_events(after_id=0, limit=20)["events"]]
    assert "code_detected" in event_types
    assert "source_attached" in event_types
    assert repository.get_source("github_repo", "992233")["item_id"] == paper_result.item_id


async def test_internal_watch_error_is_recorded_and_does_not_mark_source_checked(repository, settings) -> None:
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
                "source_uid": "2601.00111",
                "external_id": "2601.00111",
                "url": "https://arxiv.org/abs/2601.00111",
                "title": "watched",
            },
            now="2026-01-01T00:00:00Z",
            conn=connection,
        )

    async def raises(_target: str) -> SourceInspection:
        raise RuntimeError("injected internal error")

    adapters = {feed: InspectAdapter(raises) for feed in ("arxiv", "hf_papers", "hf_models", "github")}
    refresher = WatchRefresher(settings, repository, adapters, IdentityResolver(repository))
    root_run = repository.start_run(started_at="2026-11-14T00:00:00Z")

    result = await refresher.refresh_watched(root_run_id=root_run, deadline=time.monotonic() + 2)
    run = repository.list_runs(job_key="watch_refresh", limit=1)[0]

    assert result["last_status"] == "failed"
    assert run["status"] == "failed"
    assert run["ended_at"] is not None
    assert run["warnings"][0]["error_codes"] == {"INTERNAL_ERROR": 1}
    assert repository.get_source("arxiv", "2601.00111")["last_checked_at"] is None
