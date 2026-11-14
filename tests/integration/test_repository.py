from __future__ import annotations

from dataclasses import replace

from info_desk.resolver import IdentityResolver
from info_desk.sources.base import CandidateSource


def test_automatic_observation_is_idempotent_and_only_material_changes_update_item(repository) -> None:
    resolver = IdentityResolver(repository)
    candidate = CandidateSource(
        source_type="arxiv",
        source_uid="2601.00100",
        external_id="2601.00100",
        url="https://arxiv.org/abs/2601.00100",
        title="Original title",
        description="Original abstract",
    )
    with repository.transaction() as connection:
        result = resolver.ingest_candidate(
            candidate, conn=connection, now="2026-01-01T00:00:00Z", run_id=None
        )
    initial = repository.get_item(result.item_id)

    with repository.transaction() as connection:
        resolver.ingest_candidate(
            candidate, conn=connection, now="2026-01-02T00:00:00Z", run_id=None
        )
    repeated = repository.get_item(result.item_id)

    assert repeated["last_seen_at"] == "2026-01-02T00:00:00Z"
    assert repeated["updated_at"] == initial["updated_at"]
    assert repeated["revision"] == initial["revision"]
    assert len(repository.list_events(after_id=0, limit=20)["events"]) == 1

    changed = replace(candidate, title="Revised title")
    with repository.transaction() as connection:
        resolver.ingest_candidate(
            changed, conn=connection, now="2026-01-03T00:00:00Z", run_id=None
        )
    material = repository.get_item(result.item_id)

    assert material["title"] == "Revised title"
    assert material["updated_at"] == "2026-01-03T00:00:00Z"
    assert material["revision"] == repeated["revision"]
    assert [event["event_type"] for event in repository.list_events(after_id=0, limit=20)["events"]] == [
        "discovered",
        "content_changed",
    ]


def test_last_checked_at_changes_only_when_an_inspection_is_recorded(repository) -> None:
    candidate = {
        "source_type": "github_repo",
        "source_uid": "201",
        "external_id": "owner/repo",
        "url": "https://github.com/owner/repo",
        "title": "owner/repo",
        "metadata": {},
    }
    with repository.transaction() as connection:
        item_id = repository.create_item({"title": "owner/repo"}, now="2026-01-01T00:00:00Z", conn=connection)
        source_id, _ = repository.upsert_source(item_id, candidate, now="2026-01-01T00:00:00Z", conn=connection)
    repository.upsert_source(item_id, candidate, now="2026-01-02T00:00:00Z")
    unchecked = repository.get_source("github_repo", "201")
    assert unchecked["last_checked_at"] is None

    inspected = {**candidate, "last_checked_at": "2026-01-03T00:00:00Z"}
    repository.upsert_source(item_id, inspected, now="2026-01-03T00:00:00Z")
    repeated = repository.upsert_source(item_id, candidate, now="2026-01-04T00:00:00Z")
    source = repository.get_source("github_repo", "201")

    assert repeated == (source_id, False)
    assert source["last_checked_at"] == "2026-01-03T00:00:00Z"


def test_github_rename_updates_external_id_without_changing_stable_identity(repository) -> None:
    with repository.transaction() as connection:
        item_id = repository.create_item({"title": "old/repo"}, now="2026-01-01T00:00:00Z", conn=connection)
        repository.upsert_source(
            item_id,
            {
                "source_type": "github_repo",
                "source_uid": "777",
                "external_id": "old/repo",
                "url": "https://github.com/old/repo",
                "title": "old/repo",
            },
            now="2026-01-01T00:00:00Z",
            conn=connection,
        )
    repository.upsert_source(
        item_id,
        {
            "source_type": "github_repo",
            "source_uid": "777",
            "external_id": "new/repo",
            "url": "https://github.com/new/repo",
            "title": "new/repo",
        },
        now="2026-01-02T00:00:00Z",
    )

    source = repository.get_source("github_repo", "777")
    assert source["external_id"] == "new/repo"
    assert source["item_id"] == item_id
    assert repository.get_source_by_external_id("github_repo", "old/repo") is None


def test_event_source_snapshot_survives_github_rename_and_consumer_cursors(repository) -> None:
    resolver = IdentityResolver(repository)
    candidate = CandidateSource(
        source_type="github_repo",
        source_uid="778",
        external_id="old/name",
        url="https://github.com/old/name",
        title="old/name",
    )
    with repository.transaction() as connection:
        result = resolver.ingest_candidate(
            candidate, conn=connection, now="2026-01-01T00:00:00Z", run_id=None
        )
    repository.upsert_source(
        result.item_id,
        {
            **candidate.to_dict(),
            "external_id": "new/name",
            "url": "https://github.com/new/name",
            "title": "new/name",
        },
        now="2026-01-02T00:00:00Z",
    )

    consumer_a = repository.list_events(after_id=0, limit=1)
    consumer_b = repository.list_events(after_id=0, limit=1)

    assert consumer_a == consumer_b
    assert consumer_a["events"][0]["source"]["external_id"] == "old/name"
    assert consumer_a["events"][0]["source"]["url"] == "https://github.com/old/name"
    assert repository.get_source("github_repo", "778")["external_id"] == "new/name"
