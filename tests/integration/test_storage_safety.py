from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from info_desk.storage.migrations import schema_version
from info_desk.storage.repository import Repository
from info_desk.storage.sqlite import Database


def test_v1_database_migrates_once_and_snapshots_existing_event_sources(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        (Path(__file__).parents[2] / "src/info_desk/storage/sql/001_initial.sql").read_text()
    )
    connection.execute("PRAGMA user_version = 1")
    connection.execute(
        "INSERT INTO items (title, first_seen_at, last_seen_at, updated_at) "
        "VALUES ('old', '2026-01-01Z', '2026-01-01Z', '2026-01-01Z')"
    )
    connection.execute(
        """INSERT INTO sources (item_id, source_type, source_uid, external_id, url,
               first_seen_at, last_seen_at) VALUES (1, 'github_repo', '901', 'old/repo',
               'https://github.com/old/repo', '2026-01-01Z', '2026-01-01Z')"""
    )
    connection.execute(
        """INSERT INTO change_events (item_id, source_id, event_type, event_key,
               payload_json, created_at) VALUES (1, 1, 'discovered', 'old-event',
               '{}', '2026-01-01Z')"""
    )
    connection.execute(
        """INSERT INTO source_links (from_source_id, target_source_type,
               target_external_id, target_url, relation_type, evidence_level,
               evidence_origin, observed_at) VALUES
               (1, 'arxiv', '2601.00001', 'https://arxiv.org/abs/2601.00001',
                'references', 'weak', 'legacy-fixture', '2026-01-01Z')"""
    )
    connection.execute(
        """INSERT INTO sync_state (job_key, feed, query_json, coverage_from,
               completed_until, last_success_at, last_attempt_at, last_status)
               VALUES ('legacy-job', 'github', '{}', '2025-12-01Z', '2026-01-01Z',
                       '2026-01-01Z', '2026-01-01Z', 'success')"""
    )
    connection.execute(
        """INSERT INTO sync_runs (root_run_id, job_key, feed, started_at, ended_at,
               status) VALUES (NULL, 'root:github', 'github', '2026-01-01Z',
                              '2026-01-01Z', 'success')"""
    )
    connection.execute("UPDATE sync_runs SET root_run_id = id WHERE id = 1")
    connection.execute(
        """INSERT INTO sync_runs (root_run_id, job_key, feed, started_at, ended_at,
               status) VALUES (1, 'legacy-job', 'github', '2026-01-01Z',
                              '2026-01-01Z', 'success')"""
    )
    connection.execute("UPDATE change_events SET run_id = 2 WHERE event_key = 'old-event'")
    connection.commit()
    connection.close()

    repository = Repository(Database(path))
    repository.initialize()
    repository.initialize()

    with repository.database.read() as migrated:
        assert schema_version(migrated) == 3
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrated.execute(
            "SELECT count(*) FROM change_events WHERE event_key = 'old-event'"
        ).fetchone()[0] == 1
        assert tuple(migrated.execute(
            "SELECT source_type, source_uid, feed FROM sources WHERE id = 1"
        ).fetchone()) == ("github_repo", "901", "github")
        assert migrated.execute("SELECT count(*) FROM source_links WHERE id = 1").fetchone()[0] == 1
        assert migrated.execute("SELECT completed_until FROM sync_state WHERE job_key = 'legacy-job'").fetchone()[0] == "2026-01-01Z"
        assert migrated.execute("SELECT provider_cursor FROM sync_state WHERE job_key = 'legacy-job'").fetchone()[0] is None
        assert migrated.execute("SELECT root_run_id FROM sync_runs WHERE id = 2").fetchone()[0] == 1
    event = repository.list_events(after_id=0, limit=10)["events"][0]
    assert event["source"]["external_id"] == "old/repo"


def test_read_only_open_rejects_unmigrated_schema(tmp_path: Path) -> None:
    path = tmp_path / "v1-readonly.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        (Path(__file__).parents[2] / "src/info_desk/storage/sql/001_initial.sql").read_text()
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="must be migrated"):
        Repository(Database(path, readonly=True)).initialize()


def test_schema_allows_custom_identifiers_and_rejects_invalid_feed(repository) -> None:
    repository.upsert_sync_state(
        "valid-custom-feed", "test_stream", {}, now="2026-01-01T00:00:00Z"
    )
    with pytest.raises(sqlite3.IntegrityError):
        repository.upsert_sync_state(
            "invalid-feed", "test-stream", {}, now="2026-01-01T00:00:00Z"
        )
    assert repository.get_sync_state("valid-custom-feed")["feed"] == "test_stream"

    item_id = repository.create_item({"title": "custom identifier"}, now="2026-01-01T00:00:00Z")
    with pytest.raises(sqlite3.IntegrityError), repository.transaction() as connection:
        connection.execute(
            """INSERT INTO sources
               (item_id, source_type, source_uid, external_id, url,
                first_seen_at, last_seen_at, feed)
               VALUES (?, 'release-notice', '1', '1', 'https://example.test/1', ?, ?, ?)""",
            (item_id, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "test_stream"),
        )


def test_online_backup_restores_wal_database_and_never_overwrites_existing_file(
    repository, tmp_path: Path
) -> None:
    with repository.transaction() as connection:
        item_id = repository.create_item(
            {"title": "backup record"}, now="2026-01-01T00:00:00Z", conn=connection
        )
        repository.create_event(
            item_id,
            None,
            "discovered",
            "backup-event",
            {"title": "backup record"},
            created_at="2026-01-01T00:00:00Z",
            conn=connection,
        )
    destination = tmp_path / "backups" / "snapshot.db"
    backup = repository.database.backup_to(destination)

    assert backup.exists()
    assert backup.stat().st_mode & 0o777 == 0o600
    restored = Repository(Database(backup))
    restored.initialize()
    assert restored.get_item(item_id)["title"] == "backup record"
    assert restored.list_events(after_id=0, limit=10)["events"][0]["payload"] == {
        "title": "backup record"
    }
    with restored.database.read() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    before = destination.read_bytes()
    with pytest.raises(FileExistsError):
        repository.database.backup_to(destination)
    assert destination.read_bytes() == before


def test_fts5_and_like_fallback_search_english_and_chinese(repository, monkeypatch) -> None:
    with repository.transaction() as connection:
        item_id = repository.create_item(
            {"title": "Diffusion Policy 具身机器人学习"},
            now="2026-01-01T00:00:00Z",
            conn=connection,
        )

    with repository.database.read() as connection:
        has_fts = repository._has_fts(connection)
    # The repository indexes through FTS5 where available and always keeps a LIKE fallback.
    assert repository.search_items(keyword="Diffusion Policy", limit=10)[0]["item_id"] == item_id
    assert repository.search_items(keyword="具身机器人", limit=10)[0]["item_id"] == item_id

    monkeypatch.setattr(Repository, "_has_fts", staticmethod(lambda _connection: False))
    assert repository.search_items(keyword="机器人", limit=10)[0]["item_id"] == item_id
    assert isinstance(has_fts, bool)
