from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

from info_desk import cli
from info_desk.config import Settings
from info_desk.integrations.aihot import AIHOTResponse


def _run(home, *arguments):
    environment = os.environ.copy()
    environment["INFO_DESK_HOME"] = str(home)
    environment.pop("GITHUB_TOKEN", None)
    environment.pop("HF_TOKEN", None)
    return subprocess.run(
        [sys.executable, "-m", "info_desk.cli", *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )


def test_cli_initializes_diagnoses_and_serializes_offline_results(tmp_path) -> None:
    parent = tmp_path / "ordinary parent"
    parent.mkdir()
    if os.name != "nt":
        parent.chmod(0o755)
    home = parent / "isolated"
    initialized = _run(home, "init")
    assert initialized.returncode == 0
    assert json.loads(initialized.stdout)["ok"] is True
    if os.name != "nt":
        assert parent.stat().st_mode & 0o777 == 0o755
        assert home.stat().st_mode & 0o777 == 0o700

    doctor = _run(home, "doctor")
    doctor_data = json.loads(doctor.stdout)
    assert doctor.returncode == 0
    assert doctor_data["ok"] is True
    assert doctor_data["schema_version"] == 3

    search = _run(home, "search", "--keyword", "Diffusion Policy")
    assert search.returncode == 0
    assert json.loads(search.stdout) == {"items": [], "next_cursor": None}

    events = _run(home, "events", "--after-id", "0", "--limit", "10")
    event_data = json.loads(events.stdout)
    assert events.returncode == 0
    assert event_data["events"] == []
    assert event_data["next_after_id"] == 0

    conflicts = _run(home, "conflicts", "pending")
    assert conflicts.returncode == 0
    assert json.loads(conflicts.stdout) == {"pending_sources": []}


def test_cli_help_does_not_create_the_configured_data_directory(tmp_path) -> None:
    home = tmp_path / "中文 home with spaces"
    result = _run(home, "--help")
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
    assert not home.exists()


def test_cli_reports_insecure_existing_app_home_without_chmod(tmp_path) -> None:
    if os.name == "nt":
        return
    home = tmp_path / "shared home"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    result = _run(home, "doctor")
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "FILESYSTEM_PERMISSION"
    assert "set its permissions to 0700 manually" in payload["error"]["message"]
    assert home.stat().st_mode & 0o777 == 0o755


def test_cli_invalid_search_is_structured_and_has_no_traceback(tmp_path) -> None:
    _run(tmp_path / "isolated", "init")
    result = _run(tmp_path / "isolated", "search", "--limit", "0")

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_ARGUMENT"


def test_aihot_cli_can_be_explicitly_disabled_and_does_not_connect(tmp_path) -> None:
    home = tmp_path / "aihot-disabled"
    home.mkdir()
    (home / "config.toml").write_text("[aihot]\nenabled = false\n", encoding="utf-8")
    result = _run(home, "aihot", "hot-topics")
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INTEGRATION_DISABLED"
    assert "Traceback" not in result.stderr
    assert home.exists()


def test_aihot_cli_uses_the_shared_client_and_prints_json(tmp_path, monkeypatch, capsys) -> None:
    settings = Settings(
        home=tmp_path,
        aihot_enabled=True,
        aihot_timeout_sec=18,
    )
    monkeypatch.setattr(cli, "load_settings", lambda _path: settings)

    class LocalHTTPClient:
        async def aclose(self):
            return None

    monkeypatch.setattr(
        cli,
        "create_application",
        lambda **_kwargs: (SimpleNamespace(), SimpleNamespace(), LocalHTTPClient()),
    )
    calls = []

    class FakeAIHOTClient:
        def __init__(self, *, endpoint, timeout_sec):
            calls.append(("init", endpoint, timeout_sec))

        async def call_tool(self, name, arguments):
            calls.append(("call", name, arguments))
            return AIHOTResponse(ok=True, tool=name, data={"content": [], "isError": False})

    monkeypatch.setattr(cli, "AIHOTClient", FakeAIHOTClient)
    monkeypatch.setattr(
        sys,
        "argv",
        ["info-desk", "--config", str(tmp_path / "config.toml"), "aihot", "hot-topics", "--limit", "2"],
    )
    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert calls == [
        ("init", "https://aihot.news/api/mcp", 18),
        ("call", "aihot_get_hot_topics", {"limit": 2}),
    ]
