from __future__ import annotations

import pytest

from info_desk.resolver import IdentityResolver
from info_desk.sources.base import CandidateSource


def candidate(
    source_type: str,
    source_uid: str,
    *,
    external_id: str | None = None,
    links: list[dict[str, str]] | None = None,
    title: str | None = None,
) -> CandidateSource:
    return CandidateSource(
        source_type=source_type,
        source_uid=source_uid,
        external_id=external_id or source_uid,
        url=f"https://example.test/{source_uid}",
        title=title or f"Title {source_uid}",
        description=f"Summary {source_uid}",
        extracted_links=links or [],
    )


def ingest(repository, value: CandidateSource):
    resolver = IdentityResolver(repository)
    with repository.transaction() as connection:
        result = resolver.ingest_candidate(
            value, conn=connection, now="2026-01-01T00:00:00Z", run_id=None
        )
    return result


def test_arxiv_and_hf_paper_with_same_id_share_one_item(repository) -> None:
    resolver = IdentityResolver(repository)
    with repository.transaction() as connection:
        arxiv = resolver.ingest_candidate(
            candidate("arxiv", "2601.00001"),
            conn=connection,
            now="2026-01-01T00:00:00Z",
            run_id=None,
        )
        hf_paper = resolver.ingest_candidate(
            candidate("hf_paper", "2601.00001"),
            conn=connection,
            now="2026-01-02T00:00:00Z",
            run_id=None,
        )

    assert arxiv.item_id == hf_paper.item_id
    assert repository.get_item(arxiv.item_id)["sources"]
    assert repository.database.path.exists()
    assert repository.get_source("arxiv", "2601.00001") is not None
    assert repository.get_source("hf_paper", "2601.00001") is not None


def test_single_strong_official_repo_match_attaches_to_paper(repository) -> None:
    paper = ingest(repository, candidate("arxiv", "2601.00002"))
    repo = candidate(
        "github_repo",
        "1002",
        external_id="org/repo",
        links=[
            {
                "url": "https://arxiv.org/abs/2601.00002",
                "relation_type": "official_code",
                "evidence_level": "strong",
                "evidence_origin": "bidirectional-test",
            }
        ],
    )

    result = ingest(repository, repo)

    assert result.item_id == paper.item_id
    assert result.item_created is False
    assert result.source_created is True
    assert repository.get_item(paper.item_id)["sources_truncated"] is False


def test_one_way_weak_reference_does_not_merge(repository) -> None:
    paper = ingest(repository, candidate("arxiv", "2601.00003"))
    repo = candidate(
        "github_repo",
        "1003",
        links=[
            {
                "url": "https://arxiv.org/abs/2601.00003",
                "relation_type": "references",
                "evidence_level": "weak",
                "evidence_origin": "readme",
            }
        ],
    )

    result = ingest(repository, repo)

    assert result.item_id != paper.item_id
    assert repository.list_merge_conflicts() == []
    assert repository.get_source("github_repo", "1003")["item_id"] == result.item_id


def test_ignored_item_remains_deduplicated_and_keeps_manual_notes(repository) -> None:
    first = ingest(repository, candidate("arxiv", "2601.00031"))
    with repository.transaction() as connection:
        repository.update_item(
            first.item_id,
            0,
            {"triage_status": "ignored", "category": "reviewed", "notes": "do not lose"},
            now="2026-01-02T00:00:00Z",
            conn=connection,
        )
    changed = candidate("arxiv", "2601.00031", title="A revised source title")
    repeated = ingest(repository, changed)

    item = repository.get_item(first.item_id)
    assert repeated.item_id == first.item_id
    assert item["triage_status"] == "ignored"
    assert item["category"] == "reviewed"
    assert item["notes"] == "do not lose"
    assert repository.search_items(limit=20) == []
    assert repository.search_items(triage_status="ignored", limit=20)[0]["item_id"] == first.item_id


def test_source_matching_multiple_items_stays_pending_without_third_item(repository) -> None:
    first = ingest(repository, candidate("arxiv", "2601.00004"))
    second = ingest(repository, candidate("arxiv", "2601.00005"))
    repo = candidate(
        "github_repo",
        "1004",
        external_id="org/multi-paper",
        links=[
            {
                "url": "https://arxiv.org/abs/2601.00004",
                "relation_type": "official_code",
                "evidence_level": "strong",
                "evidence_origin": "repo-paper-metadata",
            },
            {
                "url": "https://arxiv.org/abs/2601.00005",
                "relation_type": "official_code",
                "evidence_level": "strong",
                "evidence_origin": "repo-paper-metadata",
            },
        ],
    )

    result = ingest(repository, repo)
    items = repository.search_items(include_ignored=True, limit=20)
    pending = repository.list_pending_sources()

    assert result.item_id is None
    assert result.pending_id is not None
    assert len(items) == 2
    assert repository.get_source("github_repo", "1004") is None
    assert pending[0]["candidate"]["external_id"] == "org/multi-paper"
    assert set(pending[0]["matched_item_ids"]) == {first.item_id, second.item_id}
    assert repository.list_merge_conflicts() == []


def test_pending_source_can_be_attached_after_explicit_review(repository) -> None:
    first = ingest(repository, candidate("arxiv", "2601.00006", title="Paper six"))
    second = ingest(repository, candidate("arxiv", "2601.00007"))
    with repository.transaction() as connection:
        repository.update_item(
            first.item_id,
            0,
            {"triage_status": "saved", "notes": "keep this note"},
            now="2026-01-02T00:00:00Z",
            conn=connection,
        )
        connection.execute("UPDATE items SET title_locked = 1 WHERE id = ?", (first.item_id,))
    repo = candidate(
        "github_repo",
        "1006",
        links=[
            {
                "url": "https://arxiv.org/abs/2601.00006",
                "relation_type": "official_code",
                "evidence_level": "strong",
                "evidence_origin": "reviewed-evidence",
            },
            {
                "url": "https://arxiv.org/abs/2601.00007",
                "relation_type": "official_code",
                "evidence_level": "strong",
                "evidence_origin": "reviewed-evidence",
            },
        ],
    )
    pending = ingest(repository, repo)

    result = IdentityResolver(repository).resolve_pending_source(
        pending.pending_id,
        first.item_id,
        now="2026-01-03T00:00:00Z",
    )

    assert result.item_id == first.item_id
    assert repository.get_source("github_repo", "1006")["item_id"] == first.item_id
    assert repository.get_item(first.item_id)["title"] == "Paper six"
    assert repository.get_item(first.item_id)["triage_status"] == "saved"
    assert repository.get_item(first.item_id)["notes"] == "keep this note"
    assert repository.get_item(second.item_id) is not None
    assert repository.list_pending_sources() == []
    events = repository.list_events(after_id=0, limit=20)["events"]
    assert any(event["event_type"] == "source_attached" for event in events)


def test_existing_source_conflict_preserves_both_items_and_is_transactional(repository) -> None:
    paper = ingest(repository, candidate("arxiv", "2601.00008"))
    repo = candidate("github_repo", "1008", external_id="org/other-paper")
    repo_result = ingest(repository, repo)
    with repository.transaction() as connection:
        repository.update_item(
            paper.item_id,
            0,
            {"notes": "paper note", "triage_status": "saved"},
            now="2026-01-02T00:00:00Z",
            conn=connection,
        )
        repository.update_item(
            repo_result.item_id,
            0,
            {"notes": "repo note", "triage_status": "saved"},
            now="2026-01-02T00:00:00Z",
            conn=connection,
        )
    linked_repo = candidate(
        "github_repo",
        "1008",
        external_id="org/other-paper",
        links=[
            {
                "url": "https://arxiv.org/abs/2601.00008",
                "relation_type": "official_code",
                "evidence_level": "strong",
                "evidence_origin": "reverse-paper-link",
            }
        ],
    )

    result = ingest(repository, linked_repo)
    before_items = [repository.get_item(paper.item_id), repository.get_item(repo_result.item_id)]
    conflict = repository.list_merge_conflicts()[0]
    with pytest.raises(ValueError, match="different notes"):
        repository.resolve_merge_conflict(
            conflict["id"], paper.item_id, now="2026-01-03T00:00:00Z"
        )
    after_items = [repository.get_item(paper.item_id), repository.get_item(repo_result.item_id)]

    assert result.item_id == repo_result.item_id
    assert result.conflict_count == 1
    assert repository.get_source("github_repo", "1008")["item_id"] == repo_result.item_id
    assert before_items == after_items
    assert repository.list_merge_conflicts()[0]["status"] == "open"
    assert len(repository.search_items(include_ignored=True, limit=20)) == 2


def test_operator_resolves_compatible_existing_item_conflict_without_losing_sources(repository) -> None:
    paper = ingest(repository, candidate("arxiv", "2601.00009", title="Same paper"))
    repo = candidate("github_repo", "1009", external_id="org/research")
    repo_item = ingest(repository, repo)
    with repository.transaction() as connection:
        repository.update_item(
            paper.item_id,
            0,
            {"triage_status": "saved", "category": "robotics", "notes": "keep"},
            now="2026-01-02T00:00:00Z",
            conn=connection,
        )
        repository.update_item(
            repo_item.item_id,
            0,
            {"triage_status": "saved", "category": "robotics", "notes": "keep"},
            now="2026-01-02T00:00:00Z",
            conn=connection,
        )
    linked = candidate(
        "github_repo",
        "1009",
        links=[
            {
                "url": "https://arxiv.org/abs/2601.00009",
                "relation_type": "official_code",
                "evidence_level": "strong",
                "evidence_origin": "bidirectional-review",
            }
        ],
    )
    result = ingest(repository, linked)
    conflict = repository.list_merge_conflicts()[0]
    merged_item_id = repository.resolve_merge_conflict(
        conflict["id"], paper.item_id, now="2026-01-03T00:00:00Z"
    )

    assert result.conflict_count == 1
    assert merged_item_id["item_id"] == paper.item_id
    assert repository.get_source("github_repo", "1009")["item_id"] == paper.item_id
    assert repository.get_item(paper.item_id)["notes"] == "keep"
    assert repository.get_item(repo_item.item_id)["triage_status"] == "ignored"
    resolved_conflict = repository.list_merge_conflicts(status="resolved")[0]
    assert resolved_conflict["status"] == "resolved"
    assert resolved_conflict["resolved_item_id"] == paper.item_id


def test_pending_resolution_rejects_unknown_resolved_and_unmatched_candidates(repository) -> None:
    match = ingest(repository, candidate("arxiv", "2601.00010"))
    resolver = IdentityResolver(repository)
    pending_candidate = candidate("github_repo", "1010", external_id="org/review")
    with repository.transaction() as connection:
        pending_id, created = repository.upsert_pending_source(
            pending_candidate.to_dict(), {"evidence": "manual"}, [match.item_id],
            now="2026-01-02T00:00:00Z", conn=connection,
        )
    assert created is True
    with pytest.raises(ValueError, match="not one of"):
        resolver.resolve_pending_source(pending_id, match.item_id + 100, now="2026-01-03T00:00:00Z")
    with pytest.raises(ValueError, match="not found"):
        resolver.resolve_pending_source(pending_id + 100, match.item_id, now="2026-01-03T00:00:00Z")
    attached = resolver.resolve_pending_source(pending_id, match.item_id, now="2026-01-03T00:00:00Z")
    assert attached.item_id == match.item_id
    with pytest.raises(ValueError, match="already resolved"):
        resolver.resolve_pending_source(pending_id, match.item_id, now="2026-01-04T00:00:00Z")


def test_pending_source_upsert_accumulates_distinct_evidence_and_matches(repository) -> None:
    payload = candidate("github_repo", "1011", external_id="org/multi").to_dict()
    first, created = repository.upsert_pending_source(
        payload, {"evidence": "first"}, [3, 4], now="2026-01-01T00:00:00Z"
    )
    repeated, created_again = repository.upsert_pending_source(
        payload, {"evidence": "first"}, [4], now="2026-01-02T00:00:00Z"
    )
    updated, created_third = repository.upsert_pending_source(
        payload, {"evidence": "second"}, [5], now="2026-01-03T00:00:00Z"
    )
    pending = repository.get_pending_source(first)
    assert created is True
    assert created_again is False
    assert created_third is False
    assert repeated == updated == first
    assert len(pending["evidence"]) == 2
    assert pending["matched_item_ids"] == [3, 4, 5]


def test_normalized_links_accept_only_supported_identity_paths(repository) -> None:
    linked = candidate(
        "github_repo",
        "1012",
        links=[
            {"url": "https://github.com/Org/Repo.git/tree/main", "evidence_level": "strong"},
            {"url": "https://huggingface.co/papers/2401.00012v2"},
            {"url": "https://huggingface.co/org/model"},
            {"url": "https://arxiv.org/abs/hep-th/9901001"},
            {"url": "https://example.com/not-a-source"},
            {"evidence_level": "strong"},
            {"url": "https://arxiv.org/abs/not-an-id"},
        ],
    )
    normalized = IdentityResolver.normalized_links(linked)
    assert [(link["target_source_type"], link["target_external_id"]) for link in normalized] == [
        ("github_repo", "Org/Repo"),
        ("hf_paper", "2401.00012"),
        ("hf_model", "org/model"),
        ("arxiv", "hep-th/9901001"),
    ]
