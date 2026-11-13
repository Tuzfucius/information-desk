from __future__ import annotations

import sqlite3
from importlib.resources import files

SCHEMA_VERSION = 1


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
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif current != SCHEMA_VERSION:
            raise SchemaMismatchError(f"No migration path from schema {current}")
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
            "CREATE VIRTUAL TABLE items_fts USING fts5(title, summary, notes, "
            "content='items', content_rowid='id')",
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


def schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])
