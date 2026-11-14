from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from info_desk.platform_utils import ensure_parent_directory, ensure_private_file


class Database:
    """Small SQLite connection and transaction manager."""

    def __init__(self, path: str | Path, *, readonly: bool = False, busy_timeout_ms: int = 5000):
        self.path = Path(path).expanduser()
        self.readonly = readonly
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self) -> sqlite3.Connection:
        if self.readonly:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout_ms / 1000)
        else:
            ensure_parent_directory(self.path.parent)
            connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
            try:
                ensure_private_file(self.path)
            except BaseException:
                connection.close()
                raise
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        if self.readonly:
            raise RuntimeError("Cannot start a write transaction on a read-only database")
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def backup_to(self, destination: str | Path) -> Path:
        """Create a consistent backup, including committed WAL data."""
        target = Path(destination).expanduser()
        ensure_parent_directory(target.parent)
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            with self.read() as source:
                backup = sqlite3.connect(target)
                try:
                    source.backup(backup)
                finally:
                    backup.close()
                ensure_private_file(target)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target
