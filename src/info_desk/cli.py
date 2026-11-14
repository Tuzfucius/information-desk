"""Command-line entry point for local operations and MCP hosting."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from info_desk.app import create_application
from info_desk.config import load_settings
from info_desk.contracts import (
    EventsRequest,
    FetchRequest,
    InspectRequest,
    SaveRequest,
    SearchRequest,
    StatusRequest,
)
from info_desk.exceptions import InfoDeskError
from info_desk.server import serve
from info_desk.storage.migrations import SCHEMA_VERSION


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Value is not JSON serializable: {type(value).__name__}")


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=_json_default))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_and_close(awaitable: Any, client: Any) -> Any:
    async def run() -> Any:
        try:
            return await awaitable
        finally:
            await client.aclose()

    return asyncio.run(run())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="info-desk")
    parser.add_argument("--config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize the data directory and SQLite schema.")
    subparsers.add_parser("doctor", help="Check local configuration and database capabilities.")

    serve_parser = subparsers.add_parser("serve", help="Run the MCP stdio server.")
    serve_parser.add_argument("--transport", choices=["stdio"], default="stdio")
    serve_parser.add_argument(
        "--profile", choices=["read-only", "standard"], default="read-only"
    )

    sync = subparsers.add_parser("sync", help="Run a scheduled or bounded manual synchronization.")
    sync.add_argument("--days", type=int)
    sync.add_argument("--feed", action="append", dest="feeds")
    sync.add_argument("--topic", action="append", dest="topics")
    subparsers.add_parser("status", help="Show local synchronization coverage.")

    search = subparsers.add_parser("search", help="Search saved research items.")
    search.add_argument("--keyword")
    search.add_argument("--status", dest="triage_status", choices=["inbox", "saved", "ignored"])
    search.add_argument("--category")
    search.add_argument("--watched", action="store_true", default=None)
    search.add_argument("--code-status", choices=["unknown", "not_detected", "detected"])
    search.add_argument("--source-type", choices=["arxiv", "hf_paper", "hf_model", "github_repo"])
    search.add_argument("--limit", type=int, default=20)

    events = subparsers.add_parser("events", help="Read the durable event stream.")
    events.add_argument("--after-id", type=int, default=0)
    events.add_argument("--limit", type=int, default=50)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect a supported source.")
    inspect_parser.add_argument("target")

    triage = subparsers.add_parser("triage", help="Update an item's review fields.")
    triage.add_argument("item_id", type=int)
    triage.add_argument("--status", dest="triage_status", choices=["inbox", "saved", "ignored"])
    triage.add_argument("--category")
    triage.add_argument("--watch", action="store_true", default=None)
    triage.add_argument("--unwatch", action="store_false", dest="is_watched")
    triage.add_argument("--notes")

    backup = subparsers.add_parser("backup", help="Create a consistent SQLite backup.")
    backup.add_argument("destination", type=Path)

    conflicts = subparsers.add_parser("conflicts", help="Review source merge conflicts.")
    conflict_actions = conflicts.add_subparsers(dest="conflict_action", required=True)
    conflict_actions.add_parser("list", help="List unresolved conflicts.")
    conflict_actions.add_parser("pending", help="List sources awaiting an item assignment.")
    resolve = conflict_actions.add_parser("resolve", help="Resolve a conflict into one item.")
    resolve.add_argument("conflict_id", type=int)
    resolve.add_argument("--winner", type=int, required=True)
    attach = conflict_actions.add_parser("attach", help="Attach a reviewed pending source to an item.")
    attach.add_argument("pending_id", type=int)
    attach.add_argument("--item", type=int, required=True)
    dismiss = conflict_actions.add_parser("dismiss", help="Dismiss an incorrect match.")
    dismiss.add_argument("conflict_id", type=int)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        settings = load_settings(args.config)
    except ValueError as error:
        _print(
            {
                "ok": False,
                "error": {
                    "code": "INVALID_CONFIG",
                    "message": str(error),
                    "retryable": False,
                    "details": {},
                },
            }
        )
        raise SystemExit(2) from error

    if args.command in {"init", "doctor", "sync"}:
        service, repository, client = create_application(
            settings=settings, initialize=True
        )
    elif args.command == "serve" and args.profile == "standard":
        service, repository, client = create_application(settings=settings)
    elif args.command == "serve":
        service, repository, client = create_application(
            settings=settings, readonly=True
        )
    else:
        service, repository, client = create_application(settings=settings)

    try:
        if args.command == "init":
            settings.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            settings.home.chmod(0o700)
            _print({"ok": True, "home": settings.home, "database": settings.database_path()})
            return
        if args.command == "doctor":
            with repository.database.read() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                fts5 = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items_fts'"
                ).fetchone() is not None
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            home_mode = settings.home.stat().st_mode & 0o777
            database_mode = settings.database_path().stat().st_mode & 0o777
            run_dir = settings.home / "run"
            lock_dir_ok = not run_dir.exists() or (run_dir.stat().st_mode & 0o077) == 0
            lockfile = Path.cwd() / "uv.lock"
            checks = {
                "schema": {"ok": version == SCHEMA_VERSION, "version": version, "expected": SCHEMA_VERSION},
                "integrity": {"ok": integrity == "ok", "result": integrity},
                "foreign_keys": {"ok": foreign_key_errors == 0, "violations": foreign_key_errors},
                "home_permissions": {"ok": home_mode & 0o077 == 0, "mode": oct(home_mode)},
                "database_permissions": {"ok": database_mode & 0o077 == 0, "mode": oct(database_mode)},
                "run_lock_directory": {"ok": lock_dir_ok, "present": run_dir.exists()},
                "uv_lock": {"present": lockfile.is_file(), "path": lockfile if lockfile.is_file() else None},
                "configuration": {"ok": True, "feeds": settings.enabled_feeds},
            }
            ok = all(
                bool(check.get("ok", True))
                for check in checks.values()
            )
            _print(
                {
                    "ok": ok,
                    "home": settings.home,
                    "database": settings.database_path(),
                    "schema_version": version,
                    "fts5": fts5,
                    "feeds": settings.enabled_feeds,
                    "checks": checks,
                }
            )
            if not ok:
                raise SystemExit(1)
            return
        if args.command == "serve":
            serve(service, profile=args.profile)
            return
        if args.command == "sync":
            if args.days is None and args.feeds is None and args.topics is None:
                operation = service.sync_scheduled()
            else:
                operation = service.fetch_updates(
                    FetchRequest(
                        days=args.days or 2,
                        feeds=args.feeds,
                        topics=args.topics,
                    )
                )
            _print(_run_and_close(operation, client))
            client = None
            return
        if args.command == "status":
            _print(service.get_sync_status(StatusRequest()))
            return
        if args.command == "search":
            request = SearchRequest(
                keyword=args.keyword,
                triage_status=args.triage_status,
                category=args.category,
                is_watched=args.watched,
                code_status=args.code_status,
                source_type=args.source_type,
                limit=args.limit,
            )
            _print(_run_and_close(service.search_items(request), client))
            client = None
            return
        if args.command == "events":
            _print(
                service.list_events(
                    EventsRequest(after_id=args.after_id, limit=args.limit)
                )
            )
            return
        if args.command == "inspect":
            _print(
                _run_and_close(
                    service.inspect_source(InspectRequest(target=args.target)), client
                )
            )
            client = None
            return
        if args.command == "triage":
            current = repository.get_item(args.item_id)
            if current is None:
                raise SystemExit(f"Item {args.item_id} was not found.")
            request = SaveRequest(
                item_id=args.item_id,
                expected_revision=current["revision"],
                triage_status=args.triage_status,
                category=args.category,
                is_watched=args.is_watched,
                notes=args.notes,
            )
            _print(_run_and_close(service.save_item(request), client))
            client = None
            return
        if args.command == "backup":
            _print({"ok": True, "backup": repository.database.backup_to(args.destination)})
            return
        if args.command == "conflicts":
            if args.conflict_action == "list":
                _print({"conflicts": repository.list_merge_conflicts()})
                return
            if args.conflict_action == "pending":
                _print({"pending_sources": repository.list_pending_sources()})
                return
            if args.conflict_action == "attach":
                try:
                    result = service.resolver.resolve_pending_source(
                        args.pending_id,
                        args.item,
                        now=_now(),
                    )
                except ValueError as error:
                    raise SystemExit(str(error)) from error
                _print(
                    {
                        "ok": True,
                        "pending_source_id": args.pending_id,
                        "item_id": result.item_id,
                        "source_id": result.source_id,
                        "action": "attached",
                    }
                )
                return
            if args.conflict_action == "resolve":
                try:
                    merged = repository.resolve_merge_conflict(
                        args.conflict_id,
                        args.winner,
                        now=_now(),
                    )
                except ValueError as error:
                    raise SystemExit(str(error)) from error
                if merged is None:
                    raise SystemExit(f"Open conflict {args.conflict_id} was not found.")
                _print({"ok": True, "winner": merged})
                return
            resolved = repository.set_conflict_status(
                args.conflict_id,
                "dismissed",
                resolved_at=_now(),
            )
            if not resolved:
                raise SystemExit(f"Open conflict {args.conflict_id} was not found.")
            _print({"ok": True, "action": "dismissed", "conflict_id": args.conflict_id})
            return
    except PydanticValidationError:
        _print(
            {
                "ok": False,
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "Command arguments did not satisfy the request contract.",
                    "retryable": False,
                    "details": {},
                },
            }
        )
        raise SystemExit(2)
    except InfoDeskError as error:
        _print(
            {
                "ok": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "retry_after_seconds": error.retry_after_seconds,
                    "details": error.details,
                },
            }
        )
        raise SystemExit(2)
    finally:
        if client is not None:
            asyncio.run(client.aclose())


if __name__ == "__main__":
    main()
