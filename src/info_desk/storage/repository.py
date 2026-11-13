from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from .migrations import migrate
from .sqlite import Database

_JSON = json.dumps


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class Repository:
    """Persistence operations for entities, sources, events, and sync state."""

    def __init__(self, database: Database):
        self.database = database

    def initialize(self) -> None:
        if self.database.readonly:
            with self.database.read() as connection:
                self._check_schema(connection)
            return
        connection = self.database.connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            migrate(connection)
        finally:
            connection.close()

    @staticmethod
    def _check_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < 1:
            raise RuntimeError("Database is not initialized")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.database.transaction() as connection:
            yield connection

    @contextmanager
    def _read_connection(
        self, connection: sqlite3.Connection | None
    ) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
        else:
            with self.database.read() as opened:
                yield opened

    @contextmanager
    def _write_connection(
        self, connection: sqlite3.Connection | None
    ) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
        else:
            with self.database.transaction() as opened:
                yield opened

    def get_item(
        self, item_id: int, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        with self._read_connection(conn) as connection:
            row = connection.execute(
                """
                SELECT id AS item_id, title, summary, category, triage_status,
                       is_watched, code_status, notes, first_seen_at, last_seen_at,
                       updated_at, revision
                FROM items WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["is_watched"] = bool(item["is_watched"])
            item["sources"] = self._sources_for_item(connection, item_id, 50)
            item["sources_truncated"] = self._source_count(connection, item_id) > 50
            return item

    def search_items(
        self,
        *,
        item_id: int | None = None,
        keyword: str | None = None,
        category: str | None = None,
        triage_status: str | None = None,
        include_ignored: bool = False,
        is_watched: bool | None = None,
        code_status: str | None = None,
        source_type: str | None = None,
        published_from: str | None = None,
        published_before: str | None = None,
        first_seen_after: str | None = None,
        updated_after: str | None = None,
        limit: int = 20,
        before: tuple[str, int] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        join_fts = False

        if item_id is not None:
            clauses.append("i.id = ?")
            parameters.append(item_id)
        if keyword:
            terms = re.findall(r"[^\W_]+", keyword, flags=re.UNICODE)
            if terms:
                with self._read_connection(conn) as connection:
                    join_fts = self._has_fts(connection)
                if join_fts:
                    expression = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
                    clauses.append("i.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
                    parameters.append(expression)
                else:
                    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    pattern = f"%{escaped}%"
                    clauses.append(
                        "(i.title LIKE ? ESCAPE '\\' OR i.summary LIKE ? ESCAPE '\\' "
                        "OR i.notes LIKE ? ESCAPE '\\')"
                    )
                    parameters.extend((pattern, pattern, pattern))
        if category is not None:
            clauses.append("i.category = ?")
            parameters.append(category)
        if triage_status is not None:
            clauses.append("i.triage_status = ?")
            parameters.append(triage_status)
        elif not include_ignored:
            clauses.append("i.triage_status != 'ignored'")
        if is_watched is not None:
            clauses.append("i.is_watched = ?")
            parameters.append(int(is_watched))
        if code_status is not None:
            clauses.append("i.code_status = ?")
            parameters.append(code_status)
        if source_type or published_from or published_before:
            source_conditions = ["s.item_id = i.id"]
            if source_type:
                source_conditions.append("s.source_type = ?")
                parameters.append(source_type)
            if published_from:
                source_conditions.append("s.published_at >= ?")
                parameters.append(published_from)
            if published_before:
                source_conditions.append("s.published_at < ?")
                parameters.append(published_before)
            clauses.append(f"EXISTS (SELECT 1 FROM sources s WHERE {' AND '.join(source_conditions)})")
        if first_seen_after:
            clauses.append("i.first_seen_at > ?")
            parameters.append(first_seen_after)
        if updated_after:
            clauses.append("i.updated_at > ?")
            parameters.append(updated_after)
        if before:
            clauses.append("(i.updated_at < ? OR (i.updated_at = ? AND i.id < ?))")
            parameters.extend((before[0], before[0], before[1]))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._read_connection(conn) as connection:
            rows = connection.execute(
                f"""
                SELECT i.id AS item_id, i.title, i.summary, i.category,
                       i.triage_status, i.is_watched, i.code_status,
                       i.first_seen_at, i.updated_at, i.revision
                FROM items i
                {where}
                ORDER BY i.updated_at DESC, i.id DESC
                LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
            items = [dict(row) for row in rows[:limit]]
            for item in items:
                item["is_watched"] = bool(item["is_watched"])
                item["sources"] = self._sources_for_item(connection, item["item_id"], 5)
                item["sources_truncated"] = self._source_count(connection, item["item_id"]) > 5
            return items

    def search_items_page(
        self,
        *,
        limit: int = 20,
        conn: sqlite3.Connection | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        """Return one keyset page and indicate whether a continuation exists."""
        rows = self.search_items(limit=limit + 1, conn=conn, **filters)
        return {"items": rows[:limit], "has_more": len(rows) > limit}

    @staticmethod
    def _has_fts(connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'items_fts'"
        ).fetchone() is not None

    @staticmethod
    def _sources_for_item(
        connection: sqlite3.Connection, item_id: int, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT source_type, source_uid, external_id, url, published_at, source_updated_at
            FROM sources
            WHERE item_id = ?
            ORDER BY source_type, source_uid
            LIMIT ?
            """,
            (item_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _source_count(connection: sqlite3.Connection, item_id: int) -> int:
        return int(
            connection.execute("SELECT count(*) FROM sources WHERE item_id = ?", (item_id,)).fetchone()[0]
        )

    def get_source(
        self,
        source_type: str,
        source_uid: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._read_connection(conn) as connection:
            row = connection.execute(
                """
                SELECT * FROM sources WHERE source_type = ? AND source_uid = ?
                """,
                (source_type, source_uid),
            ).fetchone()
            return self._source_row(row) if row else None

    def get_source_by_external_id(
        self,
        source_type: str,
        external_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._read_connection(conn) as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE source_type = ? AND external_id = ?",
                (source_type, external_id),
            ).fetchone()
            return self._source_row(row) if row else None

    @staticmethod
    def _source_row(row: sqlite3.Row) -> dict[str, Any]:
        source = dict(row)
        source["metadata"] = _loads(source.pop("metadata_json"), {})
        return source

    def find_items_for_source_links(
        self,
        links: Sequence[Mapping[str, Any]],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        with self._read_connection(conn) as connection:
            for link in links:
                row = connection.execute(
                    """
                    SELECT id AS source_id, item_id, source_type, external_id
                    FROM sources WHERE source_type = ? AND external_id = ?
                    """,
                    (link["target_source_type"], link["target_external_id"]),
                ).fetchone()
                if row:
                    matches.append(dict(row))
        return matches

    def create_item(
        self,
        fields: Mapping[str, Any],
        *,
        now: str,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        allowed = {
            "title", "summary", "category", "triage_status", "is_watched",
            "code_status", "notes", "title_locked", "summary_locked",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        values.setdefault("title", "")
        values["is_watched"] = int(bool(values.get("is_watched", False)))
        values["first_seen_at"] = now
        values["last_seen_at"] = now
        values["updated_at"] = now
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self._write_connection(conn) as connection:
            cursor = connection.execute(
                f"INSERT INTO items ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            return int(cursor.lastrowid)

    def update_item(
        self,
        item_id: int,
        expected_revision: int,
        changes: Mapping[str, Any],
        *,
        now: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        allowed = {"triage_status", "category", "is_watched", "notes"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            raise ValueError("No supported item fields provided")
        if "is_watched" in values:
            values["is_watched"] = int(bool(values["is_watched"]))
        values["updated_at"] = now
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._write_connection(conn) as connection:
            cursor = connection.execute(
                f"""
                UPDATE items SET {assignments}, revision = revision + 1
                WHERE id = ? AND revision = ?
                """,
                (*values.values(), item_id, expected_revision),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT revision FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            return {"item_id": item_id, "revision": int(row["revision"])}

    def set_item_automatic_fields(
        self,
        item_id: int,
        *,
        now: str,
        title: str | None = None,
        summary: str | None = None,
        code_status: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[Any] = []
        if title is not None:
            assignments.append("title = CASE WHEN title_locked = 0 THEN ? ELSE title END")
            values.append(title)
        if summary is not None:
            assignments.append("summary = CASE WHEN summary_locked = 0 THEN ? ELSE summary END")
            values.append(summary)
        if code_status is not None:
            assignments.append("code_status = ?")
            values.append(code_status)
        if not assignments:
            return
        assignments.extend(("last_seen_at = ?", "updated_at = ?"))
        values.extend((now, now, item_id))
        with self._write_connection(conn) as connection:
            connection.execute(
                f"UPDATE items SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def upsert_source(
        self,
        item_id: int,
        source: Mapping[str, Any],
        *,
        now: str,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[int, bool]:
        fields = {
            "item_id": item_id,
            "source_type": source["source_type"],
            "source_uid": source["source_uid"],
            "external_id": source["external_id"],
            "url": source["url"],
            "title": source.get("title", ""),
            "description": source.get("description", ""),
            "published_at": source.get("published_at"),
            "source_updated_at": source.get("source_updated_at"),
            "first_seen_at": now,
            "last_seen_at": now,
            "last_checked_at": source.get("last_checked_at"),
            "content_hash": source.get("content_hash", ""),
            "metadata_json": _JSON(source.get("metadata", {}), ensure_ascii=False, sort_keys=True),
        }
        with self._write_connection(conn) as connection:
            existing = connection.execute(
                "SELECT id, item_id FROM sources WHERE source_type = ? AND source_uid = ?",
                (fields["source_type"], fields["source_uid"]),
            ).fetchone()
            if existing:
                source_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE sources SET external_id = ?, url = ?, title = ?, description = ?,
                        published_at = ?, source_updated_at = ?, last_seen_at = ?,
                        last_checked_at = COALESCE(?, last_checked_at), content_hash = ?,
                        metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        fields["external_id"], fields["url"], fields["title"],
                        fields["description"], fields["published_at"],
                        fields["source_updated_at"], now, fields["last_checked_at"],
                        fields["content_hash"], fields["metadata_json"], source_id,
                    ),
                )
                return source_id, False
            cursor = connection.execute(
                f"INSERT INTO sources ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                tuple(fields.values()),
            )
            return int(cursor.lastrowid), True

    def set_source_links(
        self,
        source_id: int,
        links: Sequence[Mapping[str, Any]],
        *,
        observed_at: str,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        with self._write_connection(conn) as connection:
            for link in links:
                connection.execute(
                    """
                    INSERT INTO source_links (
                        from_source_id, target_source_type, target_external_id, target_url,
                        relation_type, evidence_level, evidence_origin, evidence_excerpt, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(from_source_id, target_source_type, target_external_id,
                                relation_type, evidence_origin)
                    DO UPDATE SET target_url = excluded.target_url,
                                  evidence_level = excluded.evidence_level,
                                  evidence_excerpt = excluded.evidence_excerpt,
                                  observed_at = excluded.observed_at
                    """,
                    (
                        source_id, link["target_source_type"], link["target_external_id"],
                        link.get("target_url", ""), link["relation_type"],
                        link["evidence_level"], link["evidence_origin"],
                        link.get("evidence_excerpt", ""), observed_at,
                    ),
                )

    def create_event(
        self,
        item_id: int,
        source_id: int | None,
        event_type: str,
        event_key: str,
        payload: Mapping[str, Any],
        *,
        created_at: str,
        run_id: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[int, bool]:
        with self._write_connection(conn) as connection:
            cursor = connection.execute(
                """
                INSERT INTO change_events (item_id, source_id, run_id, event_type,
                                           event_key, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO NOTHING
                """,
                (
                    item_id, source_id, run_id, event_type, event_key,
                    _JSON(payload, ensure_ascii=False, sort_keys=True), created_at,
                ),
            )
            if cursor.rowcount:
                return int(cursor.lastrowid), True
            row = connection.execute(
                "SELECT id FROM change_events WHERE event_key = ?", (event_key,)
            ).fetchone()
            return int(row["id"]), False

    def list_events(
        self,
        *,
        after_id: int = 0,
        limit: int = 50,
        event_types: Sequence[str] | None = None,
        include_ignored: bool = False,
        only_watched: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        clauses = ["e.id > ?"]
        parameters: list[Any] = [after_id]
        if event_types:
            placeholders = ", ".join("?" for _ in event_types)
            clauses.append(f"e.event_type IN ({placeholders})")
            parameters.extend(event_types)
        if not include_ignored:
            clauses.append("i.triage_status != 'ignored'")
        if only_watched:
            clauses.append("i.is_watched = 1")
        where = " AND ".join(clauses)
        with self._read_connection(conn) as connection:
            rows = connection.execute(
                f"""
                SELECT e.id, e.item_id, e.source_id, e.event_type, e.payload_json, e.created_at,
                       s.source_type, s.source_uid, s.external_id, s.url
                FROM change_events e
                JOIN items i ON i.id = e.item_id
                LEFT JOIN sources s ON s.id = e.source_id
                WHERE {where}
                ORDER BY e.id ASC
                LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            events = []
            for row in rows[:limit]:
                event = dict(row)
                event["payload"] = _loads(event.pop("payload_json"), {})
                if event["source_id"] is None:
                    event["source"] = None
                else:
                    event["source"] = {
                        key: event.pop(key)
                        for key in ("source_type", "source_uid", "external_id", "url")
                    }
                event.pop("source_id", None)
                events.append(event)
            next_after_id = events[-1]["id"] if events else after_id
            return {
                "events": events,
                "next_after_id": next_after_id,
                "has_more": has_more,
            }

    def list_events_for_run(
        self,
        root_run_id: int,
        *,
        limit: int = 30,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._read_connection(conn) as connection:
            rows = connection.execute(
                """
                SELECT e.id, e.item_id, e.source_id, e.event_type, e.payload_json, e.created_at,
                       s.source_type, s.source_uid, s.external_id, s.url
                FROM change_events e
                JOIN items i ON i.id = e.item_id
                LEFT JOIN sources s ON s.id = e.source_id
                JOIN sync_runs r ON r.id = e.run_id
                WHERE r.root_run_id = ?
                ORDER BY e.id ASC
                LIMIT ?
                """,
                (root_run_id, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            events = []
            for row in rows[:limit]:
                event = dict(row)
                event["payload"] = _loads(event.pop("payload_json"), {})
                if event["source_id"] is None:
                    event["source"] = None
                else:
                    event["source"] = {
                        key: event.pop(key)
                        for key in ("source_type", "source_uid", "external_id", "url")
                    }
                event.pop("source_id", None)
                events.append(event)
            return {"events": events, "has_more": has_more}

    def add_merge_conflict(
        self,
        item_a: int,
        item_b: int,
        evidence: Mapping[str, Any],
        *,
        created_at: str,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        left, right = sorted((item_a, item_b))
        with self._write_connection(conn) as connection:
            connection.execute(
                """
                INSERT INTO merge_conflicts (left_item_id, right_item_id, evidence_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(left_item_id, right_item_id) WHERE status = 'open' DO NOTHING
                """,
                (left, right, _JSON(evidence, ensure_ascii=False, sort_keys=True), created_at),
            )
            row = connection.execute(
                """
                SELECT id FROM merge_conflicts
                WHERE left_item_id = ? AND right_item_id = ? AND status = 'open'
                """,
                (left, right),
            ).fetchone()
            return int(row["id"])

    def list_merge_conflicts(
        self, *, status: str = "open", conn: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        with self._read_connection(conn) as connection:
            rows = connection.execute(
                """
                SELECT id, left_item_id, right_item_id, evidence_json, status,
                       created_at, resolved_at
                FROM merge_conflicts WHERE status = ? ORDER BY id
                """,
                (status,),
            ).fetchall()
            result = [dict(row) for row in rows]
            for conflict in result:
                conflict["evidence"] = _loads(conflict.pop("evidence_json"), {})
            return result

    def set_conflict_status(
        self,
        conflict_id: int,
        status: str,
        *,
        resolved_at: str,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        with self._write_connection(conn) as connection:
            cursor = connection.execute(
                """
                UPDATE merge_conflicts SET status = ?, resolved_at = ?
                WHERE id = ? AND status = 'open'
                """,
                (status, resolved_at, conflict_id),
            )
            return cursor.rowcount > 0

    def resolve_merge_conflict(
        self,
        conflict_id: int,
        winner_item_id: int,
        *,
        now: str,
    ) -> dict[str, Any] | None:
        with self.transaction() as connection:
            conflict = connection.execute(
                "SELECT * FROM merge_conflicts WHERE id = ? AND status = 'open'",
                (conflict_id,),
            ).fetchone()
            if conflict is None:
                return None
            pair = {int(conflict["left_item_id"]), int(conflict["right_item_id"])}
            if winner_item_id not in pair:
                raise ValueError("winner must be one of the conflicted item IDs")
            loser_item_id = next(item_id for item_id in pair if item_id != winner_item_id)
            open_conflicts = connection.execute(
                """
                SELECT count(*) FROM merge_conflicts
                WHERE status = 'open' AND id != ?
                  AND (left_item_id IN (?, ?) OR right_item_id IN (?, ?))
                """,
                (
                    conflict_id,
                    winner_item_id,
                    loser_item_id,
                    winner_item_id,
                    loser_item_id,
                ),
            ).fetchone()[0]
            if open_conflicts:
                raise ValueError("resolve the other open conflicts for these items first")

            winner = connection.execute(
                "SELECT * FROM items WHERE id = ?", (winner_item_id,)
            ).fetchone()
            loser = connection.execute(
                "SELECT * FROM items WHERE id = ?", (loser_item_id,)
            ).fetchone()
            if winner is None or loser is None:
                raise ValueError("one of the conflict items no longer exists")

            winner_category = winner["category"]
            loser_category = loser["category"]
            if (
                winner_category != "general"
                and loser_category != "general"
                and winner_category != loser_category
            ):
                raise ValueError("items have different non-default categories")
            category = (
                loser_category
                if winner_category == "general" and loser_category != "general"
                else winner_category
            )

            winner_notes = winner["notes"]
            loser_notes = loser["notes"]
            if winner_notes and loser_notes and winner_notes != loser_notes:
                raise ValueError("items have different notes that need manual review")
            notes = winner_notes or loser_notes

            title = winner["title"]
            title_locked = winner["title_locked"]
            if (
                winner["title_locked"]
                and loser["title_locked"]
                and winner["title"] != loser["title"]
            ):
                raise ValueError("items have different locked titles")
            if not title_locked and loser["title_locked"]:
                title = loser["title"]
                title_locked = 1
            summary = winner["summary"]
            summary_locked = winner["summary_locked"]
            if (
                winner["summary_locked"]
                and loser["summary_locked"]
                and winner["summary"] != loser["summary"]
            ):
                raise ValueError("items have different locked summaries")
            if not summary_locked and loser["summary_locked"]:
                summary = loser["summary"]
                summary_locked = 1

            code_status = (
                "detected"
                if "detected" in {winner["code_status"], loser["code_status"]}
                else winner["code_status"]
            )
            is_watched = int(bool(winner["is_watched"] or loser["is_watched"]))
            connection.execute(
                """
                UPDATE items SET title = ?, title_locked = ?, summary = ?,
                    summary_locked = ?, category = ?, notes = ?, code_status = ?,
                    is_watched = ?, updated_at = ?, revision = revision + 1
                WHERE id = ?
                """,
                (
                    title,
                    title_locked,
                    summary,
                    summary_locked,
                    category,
                    notes,
                    code_status,
                    is_watched,
                    now,
                    winner_item_id,
                ),
            )
            connection.execute(
                "UPDATE sources SET item_id = ? WHERE item_id = ?",
                (winner_item_id, loser_item_id),
            )
            connection.execute(
                "UPDATE change_events SET item_id = ? WHERE item_id = ?",
                (winner_item_id, loser_item_id),
            )
            connection.execute(
                """
                UPDATE items SET triage_status = 'ignored', is_watched = 0,
                    updated_at = ?, revision = revision + 1
                WHERE id = ?
                """,
                (now, loser_item_id),
            )
            connection.execute(
                """
                UPDATE merge_conflicts SET status = 'resolved', resolved_at = ?
                WHERE id = ?
                """,
                (now, conflict_id),
            )
            return self.get_item(winner_item_id, conn=connection)

    def get_sync_state(
        self, job_key: str, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        with self._read_connection(conn) as connection:
            row = connection.execute(
                "SELECT * FROM sync_state WHERE job_key = ?", (job_key,)
            ).fetchone()
            if not row:
                return None
            state = dict(row)
            state["query"] = _loads(state.pop("query_json"), {})
            return state

    def upsert_sync_state(
        self,
        job_key: str,
        feed: str,
        query: Mapping[str, Any],
        *,
        now: str,
        coverage_from: str | None = None,
        completed_until: str | None = None,
        last_status: str | None = None,
        last_error: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        query_json = _JSON(query, ensure_ascii=False, sort_keys=True)
        with self._write_connection(conn) as connection:
            connection.execute(
                """
                INSERT INTO sync_state (job_key, feed, query_json, coverage_from,
                                        completed_until, last_attempt_at, last_status, last_error)
                VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, 'never'), COALESCE(?, ''))
                ON CONFLICT(job_key) DO UPDATE SET
                    feed = excluded.feed,
                    query_json = excluded.query_json,
                    coverage_from = COALESCE(excluded.coverage_from, sync_state.coverage_from),
                    completed_until = COALESCE(excluded.completed_until, sync_state.completed_until),
                    last_attempt_at = excluded.last_attempt_at,
                    last_status = COALESCE(?, sync_state.last_status),
                    last_error = COALESCE(?, sync_state.last_error),
                    last_success_at = CASE WHEN ? = 'success' THEN excluded.last_attempt_at
                                            ELSE sync_state.last_success_at END
                """,
                (
                    job_key, feed, query_json, coverage_from, completed_until, now,
                    last_status, last_error, last_status, last_error, last_status,
                ),
            )

    def list_sync_states(
        self, *, feeds: Sequence[str] | None = None, conn: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        with self._read_connection(conn) as connection:
            if feeds:
                placeholders = ", ".join("?" for _ in feeds)
                rows = connection.execute(
                    f"SELECT * FROM sync_state WHERE feed IN ({placeholders}) ORDER BY feed, job_key",
                    tuple(feeds),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM sync_state ORDER BY feed, job_key"
                ).fetchall()
            result = [dict(row) for row in rows]
            for state in result:
                state["query"] = _loads(state.pop("query_json"), {})
            return result

    def start_run(
        self,
        *,
        started_at: str,
        root_run_id: int | None = None,
        job_key: str = "",
        feed: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        with self._write_connection(conn) as connection:
            cursor = connection.execute(
                """
                INSERT INTO sync_runs (root_run_id, job_key, feed, window_start, window_end,
                                       started_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'running')
                """,
                (root_run_id, job_key, feed, window_start, window_end, started_at),
            )
            run_id = int(cursor.lastrowid)
            if root_run_id is None:
                connection.execute("UPDATE sync_runs SET root_run_id = ? WHERE id = ?", (run_id, run_id))
            return run_id

    def update_run(
        self,
        run_id: int,
        *,
        ended_at: str | None = None,
        status: str | None = None,
        completed_until: str | None = None,
        fetched_count: int | None = None,
        new_sources: int | None = None,
        new_items: int | None = None,
        new_events: int | None = None,
        conflict_count: int | None = None,
        warnings: Sequence[Mapping[str, Any]] | None = None,
        error_code: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        changes = {
            "ended_at": ended_at,
            "status": status,
            "completed_until": completed_until,
            "fetched_count": fetched_count,
            "new_sources": new_sources,
            "new_items": new_items,
            "new_events": new_events,
            "conflict_count": conflict_count,
            "warning_json": _JSON(warnings, ensure_ascii=False) if warnings is not None else None,
            "error_code": error_code,
        }
        changes = {key: value for key, value in changes.items() if value is not None}
        if not changes:
            return
        with self._write_connection(conn) as connection:
            connection.execute(
                f"UPDATE sync_runs SET {', '.join(f'{key} = ?' for key in changes)} WHERE id = ?",
                (*changes.values(), run_id),
            )

    def list_runs(
        self,
        *,
        job_key: str | None = None,
        root_run_id: int | None = None,
        limit: int = 20,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses = []
        parameters: list[Any] = []
        if job_key is not None:
            clauses.append("job_key = ?")
            parameters.append(job_key)
        if root_run_id is not None:
            clauses.append("root_run_id = ?")
            parameters.append(root_run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._read_connection(conn) as connection:
            rows = connection.execute(
                f"SELECT * FROM sync_runs {where} ORDER BY id DESC LIMIT ?",
                (*parameters, limit),
            ).fetchall()
            result = [dict(row) for row in rows]
            for run in result:
                run["warnings"] = _loads(run.pop("warning_json"), [])
            return result

    def list_due_watched(
        self, cutoff: str, *, limit: int = 100, conn: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        with self._read_connection(conn) as connection:
            rows = connection.execute(
                """
                SELECT s.*, i.id AS item_id, i.title AS item_title, i.revision
                FROM sources s JOIN items i ON i.id = s.item_id
                WHERE i.is_watched = 1
                  AND (s.last_checked_at IS NULL OR s.last_checked_at <= ?)
                ORDER BY COALESCE(s.last_checked_at, '') ASC, s.id ASC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
            result = []
            for row in rows:
                source = self._source_row(row)
                source["item_title"] = source.pop("item_title")
                result.append(source)
            return result
