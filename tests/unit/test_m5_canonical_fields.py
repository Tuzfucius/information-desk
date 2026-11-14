from __future__ import annotations

from info_desk.resolver import IdentityResolver
from info_desk.sources.base import CandidateSource


def _source(source_type: str, title: str, description: str) -> CandidateSource:
    uid = "2601.12345"
    is_arxiv = source_type == "arxiv"
    return CandidateSource(
        source_type=source_type,
        source_uid=uid,
        external_id=uid,
        url=(f"https://arxiv.org/abs/{uid}" if is_arxiv
             else f"https://huggingface.co/papers/{uid}"),
        title=title,
        description=description,
    )


def test_ten_alternating_source_updates_keep_canonical_fields_deterministic(repository):
    resolver = IdentityResolver(repository, {"arxiv": 100, "hf_paper": 90})
    for turn in range(10):
        candidate = (
            _source("arxiv", "Canonical paper title", "Canonical abstract")
            if turn % 2 == 0
            else _source("hf_paper", f"HF title {turn}", f"HF abstract {turn}")
        )
        with repository.transaction() as connection:
            result = resolver.ingest_candidate(
                candidate, conn=connection, now=f"2026-01-{turn + 1:02d}T00:00:00Z",
                run_id=None,
            )
        item = repository.get_item(result.item_id)
        assert item["title"] == "Canonical paper title"
        assert item["summary"] == "Canonical abstract"

    sources = repository.sources_for_item(result.item_id)
    arxiv = next(source for source in sources if source["source_type"] == "arxiv")
    with repository.database.read() as connection:
        canonical_id = connection.execute(
            "SELECT canonical_source_id FROM items WHERE id = ?", (result.item_id,)
        ).fetchone()[0]
    assert canonical_id == arxiv["id"]


def test_manual_locks_protect_fields_while_source_facts_and_events_change(repository):
    resolver = IdentityResolver(repository)
    candidate = _source("arxiv", "Original", "Original abstract")
    with repository.transaction() as connection:
        result = resolver.ingest_candidate(
            candidate, conn=connection, now="2026-01-01T00:00:00Z", run_id=None
        )
        connection.execute(
            "UPDATE items SET title = 'Manual title', summary = 'Manual summary', "
            "title_locked = 1, summary_locked = 1 WHERE id = ?", (result.item_id,)
        )

    candidate.title = "Updated source title"
    candidate.description = "Updated source abstract"
    with repository.transaction() as connection:
        updated = resolver.ingest_candidate(
            candidate, conn=connection, now="2026-01-02T00:00:00Z", run_id=None
        )

    item = repository.get_item(result.item_id)
    assert item["title"] == "Manual title"
    assert item["summary"] == "Manual summary"
    assert updated.event_count == 1
    assert [event["event_type"] for event in repository.list_events(after_id=0, limit=10)["events"]] == [
        "discovered", "content_changed"
    ]
