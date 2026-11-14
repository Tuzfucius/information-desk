from __future__ import annotations

import json
import os
import subprocess
import sys


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
    initialized = _run(tmp_path / "isolated", "init")
    assert initialized.returncode == 0
    assert json.loads(initialized.stdout)["ok"] is True

    doctor = _run(tmp_path / "isolated", "doctor")
    doctor_data = json.loads(doctor.stdout)
    assert doctor.returncode == 0
    assert doctor_data["ok"] is True
    assert doctor_data["schema_version"] == 3

    search = _run(tmp_path / "isolated", "search", "--keyword", "Diffusion Policy")
    assert search.returncode == 0
    assert json.loads(search.stdout) == {"items": [], "next_cursor": None}

    events = _run(tmp_path / "isolated", "events", "--after-id", "0", "--limit", "10")
    event_data = json.loads(events.stdout)
    assert events.returncode == 0
    assert event_data["events"] == []
    assert event_data["next_after_id"] == 0

    conflicts = _run(tmp_path / "isolated", "conflicts", "pending")
    assert conflicts.returncode == 0
    assert json.loads(conflicts.stdout) == {"pending_sources": []}


def test_cli_invalid_search_is_structured_and_has_no_traceback(tmp_path) -> None:
    _run(tmp_path / "isolated", "init")
    result = _run(tmp_path / "isolated", "search", "--limit", "0")

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
