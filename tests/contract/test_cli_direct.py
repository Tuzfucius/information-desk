from __future__ import annotations

import json
import sys

import pytest

from info_desk.cli import main
from info_desk.sources.base import CandidateSource
from info_desk.storage.repository import Repository
from info_desk.storage.sqlite import Database


def _invoke(monkeypatch, capsys, home, *arguments):
    monkeypatch.setenv("INFO_DESK_HOME", str(home))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["info-desk", *arguments])
    main()
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_cli_direct_init_doctor_status_and_backup(monkeypatch, capsys, tmp_path) -> None:
    home = tmp_path / "cli-home"
    assert _invoke(monkeypatch, capsys, home, "init")["ok"] is True
    doctor = _invoke(monkeypatch, capsys, home, "doctor")
    assert doctor["ok"] is True
    assert doctor["schema_version"] == 2
    assert doctor["checks"]["integrity"]["result"] == "ok"
    assert doctor["checks"]["foreign_keys"]["violations"] == 0
    assert doctor["checks"]["home_permissions"]["mode"] == "0o700"
    assert doctor["checks"]["database_permissions"]["mode"] == "0o600"
    status = _invoke(monkeypatch, capsys, home, "status")
    assert {job["last_status"] for job in status["jobs"]} == {"never"}
    assert len(status["jobs"]) == 4
    backup = _invoke(monkeypatch, capsys, home, "backup", str(tmp_path / "backup.db"))
    assert backup["ok"] is True


def test_cli_conflict_views_and_invalid_arguments_are_json(monkeypatch, capsys, tmp_path) -> None:
    home = tmp_path / "cli-conflicts"
    _invoke(monkeypatch, capsys, home, "init")
    assert _invoke(monkeypatch, capsys, home, "conflicts", "list")["conflicts"] == []
    assert _invoke(monkeypatch, capsys, home, "conflicts", "pending")["pending_sources"] == []
    monkeypatch.setenv("INFO_DESK_HOME", str(home))
    monkeypatch.setattr(sys, "argv", ["info-desk", "search", "--limit", "0"])
    with pytest.raises(SystemExit) as raised:
        main()
    result = json.loads(capsys.readouterr().out)
    assert raised.value.code == 2
    assert result["error"]["code"] == "INVALID_ARGUMENT"


def test_cli_invalid_config_is_structured(monkeypatch, capsys, tmp_path) -> None:
    config = tmp_path / "invalid.toml"
    config.write_text('[http]\nread_timeout_sec = "later"\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["info-desk", "--config", str(config), "doctor"])
    with pytest.raises(SystemExit) as raised:
        main()
    output = json.loads(capsys.readouterr().out)
    assert raised.value.code == 2
    assert output["error"]["code"] == "INVALID_CONFIG"


def test_cli_reviews_and_attaches_pending_source(monkeypatch, capsys, tmp_path) -> None:
    home = tmp_path / "review-home"
    _invoke(monkeypatch, capsys, home, "init")
    repository = Repository(Database(home / "intel.db"))
    with repository.transaction() as connection:
        item_id = repository.create_item(
            {"title": "reviewed item"}, now="2026-01-01T00:00:00Z", conn=connection
        )
        candidate = CandidateSource(
            source_type="github_repo", source_uid="review-1", external_id="org/review",
            url="https://github.com/org/review", title="org/review",
        )
        pending_id, _ = repository.upsert_pending_source(
            candidate.to_dict(), {"matched_sources": ["arxiv"]}, [item_id],
            now="2026-01-01T00:00:00Z", conn=connection,
        )

    pending = _invoke(monkeypatch, capsys, home, "conflicts", "pending")
    assert pending["pending_sources"][0]["id"] == pending_id
    attached = _invoke(monkeypatch, capsys, home, "conflicts", "attach", str(pending_id), "--item", str(item_id))
    assert attached["action"] == "attached"
    assert attached["item_id"] == item_id
    assert repository.get_source("github_repo", "review-1")["item_id"] == item_id
    assert repository.list_pending_sources() == []
