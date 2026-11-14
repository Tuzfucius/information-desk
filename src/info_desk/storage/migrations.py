from __future__ import annotations

import json
import sqlite3
from importlib.resources import files

SCHEMA_VERSION = 2


class SchemaMismatchError(RuntimeError):
    pass


def migrate(connection: sqlite3.Connection) -> None:
    """Apply the schema while holding SQLite's write lock."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise SchemaMismatchError(
                f"Database schema {current} is newer than supported schema {SCHEMA_VERSION}"
            )
        if current == 0:
            sql = files("info_desk.storage").joinpath("sql/001_initial.sql").read_text(
                encoding="utf-8"
            )
            _execute_script(connection, sql)
            current = 1
        if current == 1:
            sql = files("info_desk.storage").joinpath("sql/002_reliability.sql").read_text(
                encoding="utf-8"
            )
            _execute_script(connection, sql)
            _snapshot_existing_events(connection)
            current = 2
        if current != SCHEMA_VERSION:
            raise SchemaMismatchError(f"No migration path from schema {current}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _ensure_fts(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _execute_script(connection: sqlite3.Connection, sql: str) -> None:
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("Incomplete SQL migration statement")


def _ensure_fts(connection: sqlite3.Connection) -> None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'items_fts'"
    ).fetchone()
    if exists:
        return
    connection.execute("SAVEPOINT fts_setup")
    try:
        statements = (
            (
                "CREATE VIRTUAL TABLE items_fts USING fts5(title, summary, notes, "
                "content='items', content_rowid='id')"
            ),
            """
            CREATE TRIGGER items_fts_insert AFTER INSERT ON items BEGIN
                INSERT INTO items_fts(rowid, title, summary, notes)
                VALUES (new.id, new.title, new.summary, new.notes);
            END
            """,
            """
            CREATE TRIGGER items_fts_delete AFTER DELETE ON items BEGIN
                INSERT INTO items_fts(items_fts, rowid, title, summary, notes)
                VALUES ('delete', old.id, old.title, old.summary, old.notes);
            END
            """,
            """
            CREATE TRIGGER items_fts_update AFTER UPDATE OF title, summary, notes ON items BEGIN
                INSERT INTO items_fts(items_fts, rowid, title, summary, notes)
                VALUES ('delete', old.id, old.title, old.summary, old.notes);
                INSERT INTO items_fts(rowid, title, summary, notes)
                VALUES (new.id, new.title, new.summary, new.notes);
            END
            """,
            "INSERT INTO items_fts(items_fts) VALUES ('rebuild')",
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute("RELEASE SAVEPOINT fts_setup")
    except sqlite3.OperationalError as error:
        connection.execute("ROLLBACK TO SAVEPOINT fts_setup")
        connection.execute("RELEASE SAVEPOINT fts_setup")
        message = str(error).lower()
        if "fts5" not in message and "no such module" not in message:
            raise


def _snapshot_existing_events(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT e.id, s.source_type, s.source_uid, s.external_id, s.url,
               s.published_at, s.source_updated_at
        FROM change_events e JOIN sources s ON s.id = e.source_id
        WHERE e.source_snapshot_json IS NULL
        """
    ).fetchall()
    for row in rows:
        snapshot = {
            key: row[key]
            for key in (
                "source_type", "source_uid", "external_id", "url",
                "published_at", "source_updated_at",
            )
        }
        connection.execute(
            "UPDATE change_events SET source_snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False, sort_keys=True), row["id"]),
        )


def schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])
